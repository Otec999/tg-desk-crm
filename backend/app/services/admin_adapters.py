"""Dual admin-panel transaction lookup adapters.

Two upstream admin panels (``ADMIN_ONE`` / ``ADMIN_TWO``) expose transaction
state through different APIs.  The CRM only ever talks to this module, which
normalises both of them into a single :class:`TxResult` DTO:

* :class:`BaseAdminAdapter` - the Adapter contract.  ``lookup()`` is the only
  public entry point and **never raises**: every upstream failure becomes a
  ``found=False`` result carrying an ``error_reason``.
* :class:`MockAdminMixin` - deterministic mock backend used while the real
  panels are unavailable.  Results are a pure function of ``(tx_id, panel)``
  so the same TX id always renders the same card in both panels.
* :func:`adapter_registry` / :func:`lookup_both` / :func:`lookup_one` - the
  module-level facade consumed by ``app.routers.tx``.

No real network I/O can happen with the default (mock) settings: an adapter
only performs HTTP when ``use_mock`` is ``False``, which requires a real
``http(s)`` base URL that is not an ``.example`` placeholder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from app.config import settings
from app.models import TxAdminPanel
from app.utils import mock_rng, stable_hash_int

logger = logging.getLogger("app.services.admin_adapters")

__all__ = [
    "TxResult",
    "BaseAdminAdapter",
    "MockAdminMixin",
    "AdminOneAdapter",
    "AdminTwoAdapter",
    "adapter_registry",
    "get_adapter",
    "lookup_both",
    "lookup_one",
    "mask_api_key",
    "normalize_tx_id",
    "NOT_FOUND_STATUS",
    "NOT_QUERIED_STATUS",
    "ERROR_STATUS",
]

#: Upstream used to find no transaction with this id.
NOT_FOUND_STATUS = "NOT_FOUND"
#: Adapter-level failure (timeout / transport / mapping error); not an upstream status.
ERROR_STATUS = "ERROR"
#: Panel deliberately excluded from a single-panel lookup (§3.6 still needs both cards).
NOT_QUERIED_STATUS = "NOT_QUERIED"

#: Terminal + in-flight states returned by the mocked panels.
TX_STATUS_POOL: tuple[str, ...] = (
    "COMPLETED",
    "PROCESSING",
    "PENDING",
    "FAILED",
    "DECLINED",
    "REFUNDED",
)
#: Relative weights for the deterministic "random" status pick.
_STATUS_WEIGHTS: dict[str, int] = {
    "COMPLETED": 34,
    "PROCESSING": 18,
    "PENDING": 16,
    "FAILED": 14,
    "DECLINED": 10,
    "REFUNDED": 8,
}
#: Statuses that must always explain themselves via ``error_reason``.
_ERROR_STATUSES: frozenset[str] = frozenset({"FAILED", "DECLINED"})

PROVIDER_POOL: tuple[str, ...] = (
    "AcmePayout",
    "PayGate",
    "SwiftBank",
    "CryptoBridge",
    "NovaPay",
)
CURRENCY_POOL: tuple[str, ...] = ("USDT", "EUR", "USD", "GBP")
#: Reported for a panel whose currency is unknown (absent transaction / upstream error).
DEFAULT_CURRENCY = "USDT"

_ERROR_REASONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    "FAILED": (
        "Insufficient provider balance",
        "Provider gateway timeout",
        "Invalid beneficiary IBAN",
        "Risk engine blocked the payout",
    ),
    "DECLINED": (
        "Bank rejected: insufficient funds on merchant settlement account",
        "Bank rejected: account frozen",
        "Bank rejected: AML screening failure",
        "Merchant settlement limit exceeded",
    ),
}

#: Admin-panel specific ``external_ref`` prefixes (``gw_`` style).
_EXTERNAL_REF_PREFIX: dict[str, str] = {
    TxAdminPanel.ADMIN_ONE.value: "gw_",
    TxAdminPanel.ADMIN_TWO.value: "bnk_",
}

#: Prefix that always resolves to "not found" on both panels.
_NOT_FOUND_PREFIX = "TX-0000"


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (local twin of :func:`app.utils.utcnow`)."""
    return datetime.now(timezone.utc)


def normalize_tx_id(tx_id: str) -> str:
    """Collapse separators/whitespace, strip and upper-case a transaction id.

    ``"  tx 4412 dd "`` -> ``"TX-4412-DD"``.  Already-clean ids are unchanged.
    """
    collapsed = "-".join(part for part in tx_id.strip().split() if part)
    return "-".join(chunk for chunk in collapsed.strip("-").split("-") if chunk).upper()


def mask_api_key(key: str | None) -> str:
    """Return a log-safe representation of an API key (never the full secret)."""
    if not key:
        return "<unset>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _enum_value(panel: Any) -> str:
    """Extract the raw string value from a (possibly str) enum member."""
    return str(getattr(panel, "value", panel))


def _to_float(value: Any) -> float:
    """Best-effort conversion of an upstream amount to ``float`` (``0.0`` on junk)."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _iso_z(value: Any) -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix, for the mocked ``raw`` payload."""
    if value is None:
        return _iso_z(_utcnow())
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if text.endswith("+00:00"):
        return f"{text[:-6]}Z"
    return text


@dataclass
class TxResult:
    """Panel-agnostic transaction lookup result (wire shape of §3.6)."""

    panel: TxAdminPanel
    found: bool
    tx_id: str
    status: str
    amount: str | None = None
    currency: str | None = None
    provider: str | None = None
    error_reason: str | None = None
    external_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the exact JSON shape frozen in CONTRACTS.md §3.6."""
        return {
            "panel": _enum_value(self.panel),
            "found": bool(self.found),
            "tx_id": self.tx_id,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "provider": self.provider,
            "error_reason": self.error_reason,
            "external_ref": self.external_ref,
            "raw": dict(self.raw or {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], default_panel: TxAdminPanel | None = None) -> TxResult:
        """Rebuild a result from :meth:`to_dict` output (tolerates missing keys)."""
        panel_raw = payload.get("panel", default_panel)
        try:
            panel = panel_raw if isinstance(panel_raw, TxAdminPanel) else TxAdminPanel(str(panel_raw))
        except ValueError as exc:
            raise ValueError(f"Unknown TxAdminPanel value: {panel_raw!r}") from exc
        tx_id = normalize_tx_id(str(payload.get("tx_id") or ""))
        status = str(payload.get("status") or NOT_FOUND_STATUS).upper()
        raw = payload.get("raw")
        return cls(
            panel=panel,
            found=bool(payload.get("found", False)),
            tx_id=tx_id,
            status=status,
            amount=None if payload.get("amount") is None else str(payload["amount"]),
            currency=None if payload.get("currency") is None else str(payload["currency"]),
            provider=None if payload.get("provider") is None else str(payload["provider"]),
            error_reason=None if payload.get("error_reason") is None else str(payload["error_reason"]),
            external_ref=None if payload.get("external_ref") is None else str(payload["external_ref"]),
            raw=dict(raw) if isinstance(raw, Mapping) else {},
        )

    @classmethod
    def not_found(cls, panel: TxAdminPanel, tx_id: str, *, status: str = NOT_FOUND_STATUS) -> TxResult:
        """Result describing a transaction the panel does not know about."""
        return cls(
            panel=panel,
            found=False,
            tx_id=tx_id,
            status=status,
            currency=DEFAULT_CURRENCY,
            raw={
                "tx_id": tx_id,
                "state": status,
                "panel": _enum_value(panel),
                "queried_at": _iso_z(_utcnow()),
            },
        )


class BaseAdminAdapter(ABC):
    """Adapter contract for one upstream admin panel."""

    panel: TxAdminPanel
    #: Suffix of the ``*_BASE_URL`` setting this adapter reads.
    base_url_setting: str = ""
    #: Suffix of the ``*_API_KEY`` setting this adapter reads.
    api_key_setting: str = ""
    #: Per-request transport timeout in seconds.
    timeout_s: float = 8.0

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        # Mock unless a real-looking endpoint is configured.
        self.use_mock: bool = not self.base_url.startswith(("http://", "https://")) or (
            "example" in self.base_url.lower()
        )
        logger.debug(
            "adapter %s initialised (base_url=%s, api_key=%s, use_mock=%s)",
            _enum_value(self.panel),
            self.base_url or "<unset>",
            mask_api_key(self.api_key),
            self.use_mock,
        )

    @property
    def label(self) -> str:
        """Human readable adapter name used in log records."""
        return f"{type(self).__name__}[{_enum_value(self.panel)}]"

    @abstractmethod
    async def fetch(self, tx_id: str) -> TxResult:
        """Query the upstream panel for an already normalised ``tx_id``."""
        raise NotImplementedError

    async def lookup(self, tx_id: str) -> TxResult:
        """Normalise ``tx_id``, query the panel and never raise.

        Transport errors, timeouts and unexpected exceptions are converted into
        a ``found=False`` / ``status="ERROR"`` result so a single failing panel
        can never break the dual-panel REST response.
        """
        normalized = normalize_tx_id(tx_id)
        if not normalized:
            return TxResult.not_found(self.panel, "")

        started = time.perf_counter()
        try:
            result = await self.fetch(normalized)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "%s lookup timed out after %.2fs for tx_id=%s",
                self.label,
                self.timeout_s,
                normalized,
            )
            return self._error(normalized, "upstream_timeout", started)
        except NotImplementedError:
            logger.exception("%s does not implement fetch()", self.label)
            return self._error(normalized, "adapter_not_implemented", started)
        except Exception as exc:  # noqa: BLE001 - lookup() must never propagate
            logger.warning(
                "%s lookup failed for tx_id=%s: %s: %s",
                self.label,
                normalized,
                type(exc).__name__,
                exc,
            )
            message = str(exc).strip() or type(exc).__name__
            return self._error(normalized, f"upstream_error: {message}"[:300], started)

        if not isinstance(result, TxResult):
            logger.error(
                "%s.fetch returned %s instead of TxResult", self.label, type(result).__name__
            )
            return self._error(normalized, "upstream_error: malformed adapter response", started)

        logger.debug(
            "%s resolved tx_id=%s -> status=%s in %.1f ms",
            self.label,
            normalized,
            result.status,
            (time.perf_counter() - started) * 1000.0,
        )
        return result

    def _error(self, tx_id: str, reason: str, started: float) -> TxResult:
        """Build the uniform failure result for :meth:`lookup`."""
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return TxResult(
            panel=self.panel,
            found=False,
            tx_id=tx_id,
            status=ERROR_STATUS,
            currency=DEFAULT_CURRENCY,
            error_reason=reason,
            raw={
                "tx_id": tx_id,
                "state": ERROR_STATUS,
                "panel": _enum_value(self.panel),
                "lookup_ms": elapsed_ms,
                "queried_at": _iso_z(_utcnow()),
            },
        )


class MockAdminMixin:
    """Deterministic mock upstream shared by both panel adapters.

    Every value is derived from ``stable_hash_int`` / ``mock_rng`` keyed by the
    normalised ``tx_id`` plus the concrete adapter class name, therefore the
    same TX id always yields the same card (and differs between panels).
    """

    #: Curated multi-panel stories: ``tx_id -> panel -> field overrides``.
    CURATED_DATASET: dict[str, dict[str, dict[str, Any]]] = {
        "TX-4412-DD": {
            TxAdminPanel.ADMIN_ONE.value: {
                "status": "FAILED",
                "amount": "1500.00",
                "currency": "USDT",
                "provider": "AcmePayout",
                "error_reason": "Insufficient provider balance",
                "external_ref": "gw_7f3a91",
            },
            TxAdminPanel.ADMIN_TWO.value: {
                "status": "DECLINED",
                "amount": "1500.00",
                "currency": "USDT",
                "provider": "AcmePayout",
                "error_reason": "Bank rejected: insufficient funds on merchant settlement account",
                "external_ref": "bnk_c40d82",
            },
        },
        "TX-7781-AC": {
            TxAdminPanel.ADMIN_ONE.value: {
                "status": "COMPLETED",
                "amount": "240.50",
                "currency": "EUR",
                "provider": "SwiftBank",
                "error_reason": None,
                "external_ref": "gw_1b9e44",
            },
            TxAdminPanel.ADMIN_TWO.value: {
                "status": "PROCESSING",
                "amount": "240.50",
                "currency": "EUR",
                "provider": "SwiftBank",
                "error_reason": None,
                "external_ref": "bnk_9a2f10",
            },
        },
        "TX-1102-BE": {
            TxAdminPanel.ADMIN_ONE.value: {
                "status": "PROCESSING",
                "amount": "89.99",
                "currency": "USD",
                "provider": "PayGate",
                "error_reason": None,
                "external_ref": "gw_5ce230",
            },
            TxAdminPanel.ADMIN_TWO.value: {
                "status": "PENDING",
                "amount": "89.99",
                "currency": "USD",
                "provider": "PayGate",
                "error_reason": None,
                "external_ref": "bnk_77ab35",
            },
        },
        "TX-9931-ZQ": {
            TxAdminPanel.ADMIN_ONE.value: {
                "status": "REFUNDED",
                "amount": "3120.00",
                "currency": "USDT",
                "provider": "CryptoBridge",
                "error_reason": None,
                "external_ref": "gw_refund_4d81",
            },
            TxAdminPanel.ADMIN_TWO.value: {
                "status": "REFUNDED",
                "amount": "3120.00",
                "currency": "USDT",
                "provider": "CryptoBridge",
                "error_reason": None,
                "external_ref": "bnk_refund_2ba9",
            },
        },
        "TX-5520-KM": {
            TxAdminPanel.ADMIN_ONE.value: {
                "status": "DECLINED",
                "amount": "640.00",
                "currency": "GBP",
                "provider": "NovaPay",
                "error_reason": "Risk engine blocked the payout",
                "external_ref": "gw_3ef7a2",
            },
            TxAdminPanel.ADMIN_TWO.value: {
                "status": "FAILED",
                "amount": "640.00",
                "currency": "GBP",
                "provider": "NovaPay",
                "error_reason": "Merchant settlement limit exceeded",
                "external_ref": "bnk_6d0c14",
            },
        },
    }

    panel: TxAdminPanel
    base_url: str
    api_key: str

    def _mock_payload(self, tx_id: str) -> TxResult:
        """Build the deterministic :class:`TxResult` for ``tx_id`` on this panel."""
        if tx_id.startswith(_NOT_FOUND_PREFIX):
            return TxResult.not_found(self.panel, tx_id)

        curated = self.CURATED_DATASET.get(tx_id, {}).get(_enum_value(self.panel))
        if curated is not None:
            story = dict(curated)
            amount = str(story["amount"])
            currency = str(story["currency"])
            provider = str(story["provider"])
            status = str(story["status"])
            error_reason = story.get("error_reason")
            external_ref = story.get("external_ref")
        else:
            rng = mock_rng(tx_id, salt=type(self).__name__)
            status = _weighted_status(rng)
            currency = CURRENCY_POOL[rng.randrange(len(CURRENCY_POOL))]
            provider = PROVIDER_POOL[rng.randrange(len(PROVIDER_POOL))]
            amount = f"{rng.uniform(5.0, 9500.0):.2f}"
            reasons = _ERROR_REASONS_BY_STATUS.get(status, ())
            error_reason = reasons[rng.randrange(len(reasons))] if reasons else None
            external_ref = f"{_EXTERNAL_REF_PREFIX.get(_enum_value(self.panel), 'ref_')}{rng.getrandbits(24):06x}"

        created = self._mock_created_at(tx_id)
        trace_id = f"trc_{stable_hash_int(tx_id + _enum_value(self.panel)) & 0xFFFFFF:06x}"

        return TxResult(
            panel=self.panel,
            found=True,
            tx_id=tx_id,
            status=status,
            amount=amount,
            currency=currency,
            provider=provider,
            error_reason=error_reason,
            external_ref=external_ref,
            raw={
                "tx_id": tx_id,
                "state": status,
                "amount": {"value": amount, "currency": currency},
                "provider": provider,
                "error_reason": error_reason,
                "external_ref": external_ref,
                "created_at": _iso_z(created),
                "trace_id": trace_id,
                "panel": _enum_value(self.panel),
                "mock": True,
            },
        )

    def _mock_created_at(self, tx_id: str) -> datetime:
        """Deterministic creation timestamp in the last ~14 days (UTC, tz-aware)."""
        rng = mock_rng(tx_id, salt=f"{type(self).__name__}:created")
        delta_seconds = rng.randint(0, 14 * 24 * 3600)
        return _utcnow().replace(microsecond=0) - timedelta(seconds=delta_seconds)


def _weighted_status(rng: Any) -> str:
    """Pick a status from :data:`TX_STATUS_POOL` using the configured weights."""
    total = sum(_STATUS_WEIGHTS.get(status, 1) for status in TX_STATUS_POOL)
    roll = rng.randrange(total)
    cursor = 0
    for status in TX_STATUS_POOL:
        cursor += _STATUS_WEIGHTS.get(status, 1)
        if roll < cursor:
            return status
    return TX_STATUS_POOL[-1]


class AdminOneAdapter(BaseAdminAdapter, MockAdminMixin):
    """Adapter for the first admin panel (``ADMIN_ONE_BASE_URL``)."""

    panel = TxAdminPanel.ADMIN_ONE
    base_url_setting = "admin_one_base_url"
    api_key_setting = "admin_one_api_key"
    timeout_s = 8.0

    async def fetch(self, tx_id: str) -> TxResult:
        """Query ADMIN_ONE for ``tx_id`` (mock now, HTTP when a real URL is set)."""
        if self.use_mock:
            return self._mock_payload(tx_id)

        # PRODUCTION SWAP: replace the block below with the real upstream call.
        #
        #     import httpx
        #
        #     url = f"{self.base_url}/transactions/{quote(tx_id, safe='')}"
        #     async with httpx.AsyncClient(timeout=self.timeout_s) as client:
        #         response = await client.get(
        #             url,
        #             headers={
        #                 "Authorization": f"Bearer {self.api_key}",
        #                 "Accept": "application/json",
        #             },
        #         )
        #     if response.status_code == 404:
        #         return TxResult.not_found(self.panel, tx_id)
        #     response.raise_for_status()          # -> httpx.HTTPStatusError -> lookup() maps it
        #     payload = response.json()
        #     # ADMIN_ONE envelope: {"data": {"tx_id", "state", "amount": {"value", "currency"},
        #     #                               "provider", "failure_reason", "gateway_ref",
        #     #                               "created_at", "trace_id"}}
        #     body = payload.get("data") or payload
        #     amount = body.get("amount") or {}
        #     if isinstance(amount, dict):
        #         value, currency = amount.get("value"), amount.get("currency")
        #     else:
        #         value, currency = amount, body.get("currency")
        #     state = str(body.get("state") or "").upper()
        #     return TxResult(
        #         panel=self.panel,
        #         found=state not in {"", "NOT_FOUND", "MISSING"},
        #         tx_id=str(body.get("tx_id") or tx_id).upper(),
        #         status=state or NOT_FOUND_STATUS,
        #         amount=None if value is None else f"{_to_float(value):.2f}",
        #         currency=currency,
        #         provider=body.get("provider"),
        #         error_reason=body.get("failure_reason"),
        #         external_ref=body.get("gateway_ref"),
        #         raw=body if isinstance(body, dict) else {},
        #     )
        #
        # Any exception raised here is caught by BaseAdminAdapter.lookup(), which
        # returns status="ERROR" - never propagate, the dual lookup must survive
        # a single dead panel.
        raise NotImplementedError(
            "ADMIN_ONE live transport is not configured; set ADMIN_ONE_BASE_URL or use mock mode"
        )


class AdminTwoAdapter(BaseAdminAdapter, MockAdminMixin):
    """Adapter for the second admin panel (``ADMIN_TWO_BASE_URL``)."""

    panel = TxAdminPanel.ADMIN_TWO
    base_url_setting = "admin_two_base_url"
    api_key_setting = "admin_two_api_key"
    timeout_s = 8.0

    async def fetch(self, tx_id: str) -> TxResult:
        """Query ADMIN_TWO for ``tx_id`` (mock now, HTTP when a real URL is set)."""
        if self.use_mock:
            return self._mock_payload(tx_id)

        # PRODUCTION SWAP: replace the block below with the real upstream call.
        #
        #     import httpx
        #
        #     url = f"{self.base_url}/v1/transactions/status"
        #     async with httpx.AsyncClient(timeout=self.timeout_s) as client:
        #         response = await client.get(
        #             url,
        #             params={"reference": tx_id},
        #             headers={
        #                 "Authorization": f"Bearer {self.api_key}",
        #                 "Accept": "application/json",
        #             },
        #         )
        #     if response.status_code == 404:
        #         return TxResult.not_found(self.panel, tx_id)
        #     response.raise_for_status()
        #     payload = response.json()
        #     # ADMIN_TWO envelope: {"result": {"reference", "status", "amount_value",
        #     #                                 "amount_currency", "bank_provider",
        #     #                                 "decline_reason", "bank_ref", "booked_at"}}
        #     body = payload.get("result") or payload
        #     return TxResult(
        #         panel=self.panel,
        #         found=bool(body),
        #         tx_id=str(body.get("reference") or tx_id).upper(),
        #         status=str(body.get("status") or NOT_FOUND_STATUS).upper(),
        #         amount=None if body.get("amount_value") is None
        #         else f"{_to_float(body['amount_value']):.2f}",
        #         currency=body.get("amount_currency"),
        #         provider=body.get("bank_provider"),
        #         error_reason=body.get("decline_reason"),
        #         external_ref=body.get("bank_ref"),
        #         raw=body if isinstance(body, dict) else {},
        #     )
        #
        # Exceptions are normalised by BaseAdminAdapter.lookup() into
        # status="ERROR" with an "upstream_error: ..." reason.
        raise NotImplementedError(
            "ADMIN_TWO live transport is not configured; set ADMIN_TWO_BASE_URL or use mock mode"
        )


_ADAPTER_CLASSES: tuple[type[BaseAdminAdapter], ...] = (AdminOneAdapter, AdminTwoAdapter)

_registry: dict[TxAdminPanel, BaseAdminAdapter] | None = None
_registry_lock = Lock()


def _build_registry() -> dict[TxAdminPanel, BaseAdminAdapter]:
    """Instantiate one adapter per panel from the ``settings`` object."""
    built: dict[TxAdminPanel, BaseAdminAdapter] = {}
    for adapter_cls in _ADAPTER_CLASSES:
        base_url = str(getattr(settings, adapter_cls.base_url_setting, "") or "")
        api_key = str(getattr(settings, adapter_cls.api_key_setting, "") or "")
        built[adapter_cls.panel] = adapter_cls(base_url=base_url, api_key=api_key)
    return built


def adapter_registry() -> dict[TxAdminPanel, BaseAdminAdapter]:
    """Return the process-wide adapter registry (lazily built, thread-safe)."""
    global _registry
    cached = _registry
    if cached is not None:
        return cached
    with _registry_lock:
        if _registry is None:
            _registry = _build_registry()
            logger.info(
                "admin adapter registry ready: %s",
                ", ".join(
                    f"{_enum_value(panel)}={'mock' if adapter.use_mock else 'live'}"
                    for panel, adapter in _registry.items()
                ),
            )
        return _registry


def _coerce_panel(panel: TxAdminPanel | str) -> TxAdminPanel:
    """Accept either the enum or its raw string value."""
    if isinstance(panel, TxAdminPanel):
        return panel
    return TxAdminPanel(str(panel).strip().upper())


def get_adapter(panel: TxAdminPanel | str) -> BaseAdminAdapter:
    """Look up the adapter for one panel (``KeyError`` -> ``ValueError``)."""
    registry = adapter_registry()
    try:
        return registry[_coerce_panel(panel)]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"No admin adapter registered for panel {panel!r}") from exc


async def lookup_both(tx_id: str) -> dict[TxAdminPanel, TxResult]:
    """Query both panels concurrently; a failing panel never cancels the other."""
    normalized = normalize_tx_id(tx_id)
    registry = adapter_registry()
    panels = list(registry)
    results = await asyncio.gather(
        *(registry[panel].lookup(normalized) for panel in panels),
        return_exceptions=True,
    )
    out: dict[TxAdminPanel, TxResult] = {}
    for panel, outcome in zip(panels, results, strict=True):
        if isinstance(outcome, TxResult):
            out[panel] = outcome
            continue
        logger.warning(
            "adapter %s raised outside lookup() guard: %s: %s",
            _enum_value(panel),
            type(outcome).__name__,
            outcome,
        )
        out[panel] = TxResult(
            panel=panel,
            found=False,
            tx_id=normalized,
            status=ERROR_STATUS,
            error_reason=f"upstream_error: {type(outcome).__name__}",
            raw={"tx_id": normalized, "state": ERROR_STATUS, "panel": _enum_value(panel)},
        )
    return out


async def lookup_one(panel: TxAdminPanel | str, tx_id: str) -> TxResult:
    """Query a single panel; never raises for upstream failures."""
    return await get_adapter(panel).lookup(normalize_tx_id(tx_id))

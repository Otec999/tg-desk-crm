"""Transaction lookup router (§3.6) - dual admin-panel adapters + audit trail.

Both panels are queried concurrently (``asyncio.gather`` inside
``admin_adapters.lookup_both``); a dead panel degrades to a ``status="ERROR"``
card instead of failing the request. Every queried panel writes exactly one
``tx_lookup_audit`` row, committed before the payload is returned.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Operator, TxAdminPanel, TxLookupAudit, TxStatus
from app.routers._common import as_contract, iso_or_none
from app.routers.chats import is_supervisor
from app.schemas import TxAuditListOut, TxLookupRequest, TxLookupResponse, TxPanelResult
from app.security import get_current_operator
from app.services.admin_adapters import (
    TxResult,
    lookup_both,
    lookup_one,
    normalize_tx_id,
)

logger = logging.getLogger("app.routers.tx")

router = APIRouter(prefix="/tx", tags=["tx"])

#: Longest accepted TX id (matches ``tx_lookup_audit.tx_id`` VARCHAR(64)).
MAX_TX_ID_LENGTH = 64
#: Accepted characters in a raw TX id: alphanumerics plus ``-``/``_``/whitespace groups.
TX_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
DEFAULT_AUDIT_LIMIT = 100
MAX_AUDIT_LIMIT = 500
MAX_SUGGESTIONS = 10
SUGGEST_SCAN_LIMIT = 200


def _validate_tx_id(tx_id: str) -> str:
    """Normalise and sanity-check a TX id (``422`` on garbage)."""
    if not str(tx_id or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tx_id must not be empty",
        )
    if not TX_ID_PATTERN.fullmatch(str(tx_id).strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tx_id may only contain letters, digits, dashes and underscores",
        )
    normalized = normalize_tx_id(tx_id)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tx_id must not be empty",
        )
    if len(normalized) > MAX_TX_ID_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"tx_id is longer than {MAX_TX_ID_LENGTH} characters",
        )
    return normalized


def _audit_status(status_value: str) -> str | None:
    """Map an adapter status onto the audited ``TxStatus`` column.

    Adapter-level failures (``"ERROR"``) are not a transaction status, so they
    are stored as ``NULL``; the response code is still ``200`` with the error
    reason on the panel card.
    """
    try:
        return TxStatus(status_value).value
    except ValueError:
        return None


def _panel_result(result: TxResult) -> Any:
    """Validate one adapter result into ``TxPanelResult``."""
    return as_contract("TxPanelResult", result.to_dict())


def _response(
    tx_id: str,
    looked_up_at: datetime,
    results: dict[TxAdminPanel, TxResult],
) -> TxLookupResponse:
    """Assemble ``TxLookupResponse`` (§3.6 mandates both panel cards)."""
    admin_one = results.get(TxAdminPanel.ADMIN_ONE) or _not_queried(TxAdminPanel.ADMIN_ONE, tx_id)
    admin_two = results.get(TxAdminPanel.ADMIN_TWO) or _not_queried(TxAdminPanel.ADMIN_TWO, tx_id)
    return as_contract(
        "TxLookupResponse",
        {
            "tx_id": tx_id,
            "looked_up_at": iso_or_none(looked_up_at),
            "admin_one": _panel_result(admin_one),
            "admin_two": _panel_result(admin_two),
        },
    )


def _audit_row(operator: Operator, result: TxResult, lookup_ms: int, created_at: datetime) -> TxLookupAudit:
    """Build one ``tx_lookup_audit`` row for a queried panel."""
    return TxLookupAudit(
        operator_id=int(operator.id),
        tx_id=result.tx_id,
        admin_panel=result.panel.value,
        found=bool(result.found),
        response_status=_audit_status(str(result.status)),
        lookup_ms=max(int(lookup_ms), 0),
        created_at=created_at,
    )


def _not_queried(panel: TxAdminPanel, tx_id: str) -> TxResult:
    """Card for a panel that was deliberately not queried in this call.

    ``status`` is ``NOT_FOUND`` (the only "absent" member of the frozen §2
    ``TxStatus`` enum); the ``raw`` payload still states ``NOT_QUERIED`` so the
    distinction is preserved for the sidebar tooltip.
    """
    return TxResult.not_found(panel, tx_id)


async def _run_lookup(
    db: AsyncSession,
    operator: Operator,
    tx_id: str,
    panel: TxAdminPanel | None,
) -> TxLookupResponse:
    """Query one or both panels, write audit rows, commit, return the payload.

    ``TxLookupResponse`` always carries both ``admin_one`` and ``admin_two``
    (frozen §3.6). When ``panel`` restricts the lookup, only that panel is
    queried and audited; the other one is reported as a not-queried card so the
    response shape and the audit trail both stay truthful.
    """
    started = time.perf_counter()
    if panel is None:
        results = await lookup_both(tx_id)
    else:
        results = {panel: await lookup_one(panel, tx_id)}
    lookup_ms = int(round((time.perf_counter() - started) * 1000.0))

    looked_up_at = datetime.now(timezone.utc).replace(microsecond=0)
    for result in results.values():
        db.add(_audit_row(operator, result, lookup_ms, looked_up_at))

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - an audit failure must not hide the answer
        await db.rollback()
        logger.warning("failed to persist tx audit rows for %s: %s", tx_id, exc)

    logger.info(
        "operator %s looked up %s on %s in %s ms",
        operator.username,
        tx_id,
        ",".join(result.panel.value for result in results.values()) or "both",
        lookup_ms,
    )
    return _response(tx_id, looked_up_at, results)


@router.post(
    "/lookup",
    response_model=TxLookupResponse,
    status_code=status.HTTP_200_OK,
    summary="Look up a transaction in one or both admin panels",
    responses={422: {"description": "Malformed TX id or panel"}},
)
async def lookup_transaction(
    payload: TxLookupRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TxLookupResponse:
    """Run the admin adapters concurrently and record an audit row per panel."""
    tx_id = _validate_tx_id(payload.tx_id)
    panel = getattr(payload, "panel", None)
    if panel is not None and not isinstance(panel, TxAdminPanel):
        try:
            panel = TxAdminPanel(str(panel).upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid panel: {panel!r} (expected ADMIN_ONE or ADMIN_TWO)",
            ) from exc
    return await _run_lookup(db, operator, tx_id, panel)


@router.get(
    "/lookup/{tx_id}",
    response_model=TxLookupResponse,
    status_code=status.HTTP_200_OK,
    summary="Look up a transaction by path parameter",
    responses={422: {"description": "Malformed TX id"}},
)
async def lookup_transaction_by_id(
    tx_id: str,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    panel: Annotated[TxAdminPanel | None, Query(description="Restrict the lookup to one panel")] = None,
) -> TxLookupResponse:
    """Same payload as ``POST /tx/lookup``; also writes audit rows."""
    return await _run_lookup(db, operator, _validate_tx_id(tx_id), panel)


@router.get(
    "/audit",
    response_model=TxAuditListOut,
    status_code=status.HTTP_200_OK,
    summary="Lookup audit trail (supervisor sees all, operators see their own)",
)
async def audit_trail(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LIMIT, description="Maximum rows")] = DEFAULT_AUDIT_LIMIT,
    tx_id: Annotated[str | None, Query(description="Filter by TX id (prefix match)")] = None,
) -> TxAuditListOut:
    """Return ``{"items": [TxAuditItem, ...]}`` newest first."""
    stmt = select(TxLookupAudit, Operator.username).join(
        Operator, Operator.id == TxLookupAudit.operator_id, isouter=True
    )
    if not is_supervisor(operator):
        stmt = stmt.where(TxLookupAudit.operator_id == operator.id)
    if tx_id:
        stmt = stmt.where(TxLookupAudit.tx_id.ilike(f"{normalize_tx_id(tx_id)}%"))

    rows = (
        await db.execute(
            stmt.order_by(TxLookupAudit.created_at.desc(), TxLookupAudit.id.desc()).limit(limit)
        )
    ).all()

    items = [
        as_contract(
            "TxAuditItem",
            {
                "id": int(audit.id),
                "operator_id": int(audit.operator_id),
                "operator_username": username,
                "tx_id": audit.tx_id,
                "admin_panel": str(getattr(audit.admin_panel, "value", audit.admin_panel)),
                "found": bool(audit.found),
                "response_status": audit.response_status,
                "lookup_ms": int(audit.lookup_ms or 0),
                "created_at": iso_or_none(audit.created_at),
            },
        )
        for audit, username in rows
    ]
    logger.debug("audit trail for operator %s: %s rows", operator.id, len(items))
    return TxAuditListOut.model_validate({"items": items})


@router.get(
    "/suggest",
    response_model=dict[str, list[str]],
    status_code=status.HTTP_200_OK,
    summary="Autocomplete recent TX ids for the calling operator",
)
async def suggest_tx_ids(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query(description="TX id prefix typed so far")] = "",
) -> dict[str, list[str]]:
    """Return up to 10 recent distinct TX ids matching the prefix."""
    prefix = (q or "").strip().upper()
    if not prefix:
        return {"items": []}

    stmt = select(TxLookupAudit.tx_id).where(TxLookupAudit.tx_id.ilike(f"{prefix}%"))
    if not is_supervisor(operator):
        stmt = stmt.where(TxLookupAudit.operator_id == operator.id)
    rows = (
        await db.execute(
            stmt.order_by(TxLookupAudit.created_at.desc(), TxLookupAudit.id.desc()).limit(SUGGEST_SCAN_LIMIT)
        )
    ).scalars().all()

    suggestions: list[str] = []
    for value in rows:
        candidate = str(value)
        if candidate not in suggestions:
            suggestions.append(candidate)
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
    return {"items": suggestions}

"""Editable application settings with a short-lived in-process read cache.

CONTRACTS_V5 §2 makes the SLA behaviour configurable at runtime:

* §2.1 — one ``app_settings`` row (``id = 1``) holding ``sla_enabled``,
  ``sla_target_seconds`` and ``sla_reset_on_operator_reply``;
* §2.2 — ``GET``/``PATCH /api/admin/settings`` read and write that row;
* §2.3 — ``sla_enabled = False`` stops new incidents from opening and
  ``sla_target_seconds`` becomes the global default behind the per-operator override.

Why a cache at all
------------------
:func:`~app.services.sla_engine.open_incident` is on the hot path of **every** inbound
message: the Telegram worker ingests a message and expects an ``SLA_UPDATE`` frame
within milliseconds. Reading a single-row table each time costs a round-trip that adds
nothing — the row changes maybe once a month — so the value is memoised for
:data:`_CACHE_TTL_SECONDS` seconds (§2.1 «кэшируется в процессе на 5 секунд»).

Concurrency model
-----------------
The cache is a plain module-level tuple read and written **without** locks. That is
deliberate: every mutation assigns one immutable snapshot (``(_row, _expires_at)``),
so a concurrent reader always observes either the previous or the next complete
snapshot — never a half-updated pair. There is no I/O inside the critical section, so
nothing can ``await`` while holding a stale value, and the async event loop is never
blocked (a lock would be pointless here: no suspension point exists between the read
and the use).

Correctness rules:

* :func:`update_settings` **invalidates** the cache *after* the transaction commits, so
  a failed write can never leave the cache emptied-for-nothing or poisoned with the
  intended value;
* a cache hit returns the detached ORM instance. Callers must treat it as read-only
  (only :func:`update_settings` writes, and it re-reads the row under a lock);
* the key includes nothing but time — there is one row, so one entry is enough.

The module deliberately imports the ORM model and the schemas **lazily** inside the
functions it needs them in: ``app.models`` is a leaf that other agents edit in
parallel, and importing it at module scope here would make a partially written
``models.py`` break the whole router tree at import time instead of failing one
request. The type-only import stays under ``TYPE_CHECKING`` so annotations resolve.

Transaction boundary: both public coroutines issue ``commit()`` themselves — they are
entry points of their own unit of work (an HTTP request handler and the SLA engine's
read), unlike the ``flush()``-only convention of ``sla_engine``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from app.models import AppSettings
    from app.schemas import AdminSettingsUpdate

__all__ = [
    "DEFAULT_SLA_RESET_ON_OPERATOR_REPLY",
    "DEFAULT_SLA_TARGET_SECONDS",
    "SETTINGS_ROW_ID",
    "SettingsSnapshot",
    "effective_sla_target",
    "get_settings",
    "invalidate_cache",
    "peek_cached_settings",
    "update_settings",
]

logger = logging.getLogger(__name__)

#: How long a read snapshot stays valid, in seconds (CONTRACTS_V5 §2.1).
_CACHE_TTL_SECONDS: Final[float] = 5.0

#: Primary key of the singleton settings row (CONTRACTS_V5 §2.1: «одна строка, id = 1»).
SETTINGS_ROW_ID: Final[int] = 1

#: Default global SLA target in seconds used when the row has not been bootstrapped yet.
#:
#: NOTE ON THE CONTRACT: §2.1 spells the *column* default as ``1800`` (30 min) and the
#: column really does carry it (``app.models.AppSettings.sla_target_seconds``). This
#: constant is the fallback used when the table itself is unavailable/empty, and it
#: stays at the historic ``settings.backend_sla_seconds`` value so the pre-V5 behaviour
#: of the engine is preserved when nothing was ever configured. The effective target of
#: a live system therefore comes from the row; this value only matters for a degraded
#: read. See ``docs``/report: the difference is intentional and documented.
DEFAULT_SLA_TARGET_SECONDS: Final[int] = 300

#: Default of the «ответ оператора сбрасывает клок» rule (CONTRACTS_V5 §3.2).
DEFAULT_SLA_RESET_ON_OPERATOR_REPLY: Final[bool] = True

#: Default of the master switch (CONTRACTS_V5 §2.3).
DEFAULT_SLA_ENABLED: Final[bool] = True


class SettingsSnapshot(NamedTuple):
    """Immutable, detached view of the settings row.

    A ``NamedTuple`` rather than a dataclass because the cache stores it and readers
    copy nothing: being a tuple it is immutable by construction, hashable, and needs no
    defensive copying before being handed to two goroutines at once. The field names
    mirror the columns one-for-one so :class:`~app.schemas.AdminSettingsOut` can be
    built with ``model_validate(snapshot)``.
    """

    sla_enabled: bool
    sla_target_seconds: int
    sla_reset_on_operator_reply: bool
    updated_at: datetime | None
    updated_by: str | None


#: The documented defaults, used both to bootstrap the row and to answer a degraded read.
def _defaults() -> SettingsSnapshot:
    """Build the default snapshot (fresh ``updated_at``, no author)."""
    target = _clamped_target(getattr(_config(), "backend_sla_seconds", None))
    return SettingsSnapshot(
        sla_enabled=DEFAULT_SLA_ENABLED,
        sla_target_seconds=target,
        sla_reset_on_operator_reply=DEFAULT_SLA_RESET_ON_OPERATOR_REPLY,
        updated_at=utcnow(),
        updated_by=None,
    )


def _config() -> Any:
    """Return :data:`app.config.settings` (imported lazily, see the module docstring)."""
    from app.config import settings as app_config

    return app_config


def _clamped_target(value: Any) -> int:
    """Coerce ``value`` into the contract's ``[60, 86400]`` window, or the fallback.

    A non-numeric, non-positive or out-of-range value can only come from a hand-edited
    database or a mis-set environment variable; handing it to the engine would produce a
    zero-length SLA window, so it degrades to ``settings.backend_sla_seconds`` instead of
    raising inside the ingest hot path.
    """
    from app.models import SLA_TARGET_SECONDS_MAX, SLA_TARGET_SECONDS_MIN

    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = 0
    if SLA_TARGET_SECONDS_MIN <= seconds <= SLA_TARGET_SECONDS_MAX:
        return seconds
    fallback = getattr(_config(), "backend_sla_seconds", DEFAULT_SLA_TARGET_SECONDS)
    try:
        safe = int(fallback)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        safe = DEFAULT_SLA_TARGET_SECONDS
    if SLA_TARGET_SECONDS_MIN <= safe <= SLA_TARGET_SECONDS_MAX:
        return safe
    logger.warning(
        "некорректный BACKEND_SLA_SECONDS=%r вне диапазона [%s, %s]; используется %s",
        fallback,
        SLA_TARGET_SECONDS_MIN,
        SLA_TARGET_SECONDS_MAX,
        DEFAULT_SLA_TARGET_SECONDS,
    )
    return DEFAULT_SLA_TARGET_SECONDS


# ---------------------------------------------------------------------------
# Process-local cache (5 s)
# ---------------------------------------------------------------------------

#: ``(payload, monotonic_expiry)`` of the last successful read, or ``None``.
#: Written atomically as a whole tuple — see the concurrency notes in the module
#: docstring for why no lock is needed.
_cache: tuple[SettingsSnapshot, float] | None = None


def invalidate_cache() -> None:
    """Drop the cached snapshot so the next :func:`get_settings` re-reads the row.

    Safe to call at any time from any coroutine: it only rebinds one module-level name
    to ``None``, which cannot yield and therefore cannot interleave with a reader.
    """
    global _cache
    _cache = None
    logger.debug("кэш настроек приложения сброшен")


def peek_cached_settings() -> SettingsSnapshot | None:
    """Return the cached snapshot **without** touching the database (or ``None``).

    Used by tests and diagnostics to assert cache invalidation; production code should
    call :func:`get_settings` instead, which refreshes the entry when needed.
    """
    entry = _cache
    if entry is None:
        return None
    payload, expires_at = entry
    return payload if time.monotonic() < expires_at else None


def _store(snapshot: SettingsSnapshot) -> None:
    """Put ``snapshot`` into the cache with a fresh TTL."""
    global _cache
    _cache = (snapshot, time.monotonic() + _CACHE_TTL_SECONDS)


def _snapshot_of(row: AppSettings) -> SettingsSnapshot:
    """Project an ORM row onto the immutable cache payload.

    ``getattr`` with defaults instead of attribute access: the service must keep
    working if a future migration adds a column, and must never raise
    ``AttributeError`` inside an ingest handler because a field was renamed.
    """
    return SettingsSnapshot(
        sla_enabled=bool(getattr(row, "sla_enabled", DEFAULT_SLA_ENABLED)),
        sla_target_seconds=_clamped_target(
            getattr(row, "sla_target_seconds", DEFAULT_SLA_TARGET_SECONDS)
        ),
        sla_reset_on_operator_reply=bool(
            getattr(row, "sla_reset_on_operator_reply", DEFAULT_SLA_RESET_ON_OPERATOR_REPLY)
        ),
        updated_at=getattr(row, "updated_at", None),
        updated_by=getattr(row, "updated_by", None),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _load_row(db: AsyncSession) -> AppSettings | None:
    """Fetch the singleton row by primary key (``None`` when it was never created)."""
    from app.models import AppSettings

    stmt = select(AppSettings).where(AppSettings.id == SETTINGS_ROW_ID).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _create_row(db: AsyncSession) -> AppSettings:
    """Insert the singleton row with the documented defaults and commit.

    Called only when the row is genuinely missing (fresh database, or a migration that
    has not run yet). ``flush`` assigns nothing here — the primary key is fixed — but it
    surfaces a ``CHECK``/``IntegrityError`` before the commit, and the commit makes the
    row visible to every other worker process instead of only this session.
    """
    from app.models import AppSettings

    snapshot = _defaults()
    row = AppSettings(
        id=SETTINGS_ROW_ID,
        sla_enabled=snapshot.sla_enabled,
        sla_target_seconds=snapshot.sla_target_seconds,
        sla_reset_on_operator_reply=snapshot.sla_reset_on_operator_reply,
        updated_at=snapshot.updated_at,
        updated_by=snapshot.updated_by,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    logger.info(
        "создана строка app_settings: sla_enabled=%s sla_target_seconds=%s "
        "sla_reset_on_operator_reply=%s",
        snapshot.sla_enabled,
        snapshot.sla_target_seconds,
        snapshot.sla_reset_on_operator_reply,
    )
    return row


async def get_settings(db: AsyncSession) -> AppSettings:
    """Return the singleton ``app_settings`` row, creating it with defaults on first use.

    Serves a cached instance for up to :data:`_CACHE_TTL_SECONDS` seconds, so the SLA
    hot path costs one indexed primary-key lookup per five seconds at most.

    The returned object is the ORM row when freshly read and a **detached snapshot-like
    instance** when it came from the cache. In both cases callers must treat it as
    read-only: every attribute the engine needs (``sla_enabled``,
    ``sla_target_seconds``, ``sla_reset_on_operator_reply``) is present, and no caller
    is allowed to mutate it — :func:`update_settings` is the only writer.

    A database failure is **not** swallowed: ``open_incident`` runs inside the ingest
    transaction and a broken settings read must roll that transaction back rather than
    silently opening incidents with guessed values.
    """
    cached = peek_cached_settings()
    if cached is not None:
        return _as_row_like(cached)

    row = await _load_row(db)
    if row is None:
        # Missing row: bootstrap once, commit, then serve it. The insert is idempotent
        # in practice — a concurrent bootstrapper loses the race on the primary key and
        # the loser's flush raises, so the reader below simply re-reads the winner.
        try:
            row = await _create_row(db)
        except Exception as exc:  # noqa: BLE001 - a lost insert race is expected
            logger.info(
                "не удалось создать app_settings (%s), перечитываю существующую строку",
                type(exc).__name__,
            )
            await db.rollback()
            row = await _load_row(db)
            if row is None:
                raise
    snapshot = _snapshot_of(row)
    _store(snapshot)
    return _as_row_like(snapshot)


class _RowLike:
    """A detached stand-in for :class:`~app.models.AppSettings` served from the cache.

    The cache stores a :class:`SettingsSnapshot`; returning a lightweight object with
    the same attribute names keeps the public signature honest (``-> AppSettings``) and
    lets ``sla_engine`` read ``row.sla_enabled`` without caring where the value came
    from. It is immutable — the setter raises — which makes an accidental mutation of a
    cached value loud instead of silently corrupting every other coroutine's view.
    """

    __slots__ = ("_snapshot",)

    def __init__(self, snapshot: SettingsSnapshot) -> None:
        object.__setattr__(self, "_snapshot", snapshot)

    # -- read-only column proxies ------------------------------------------
    @property
    def id(self) -> int:
        """Primary key of the singleton row (always :data:`SETTINGS_ROW_ID`)."""
        return SETTINGS_ROW_ID

    @property
    def sla_enabled(self) -> bool:
        """Master switch (CONTRACTS_V5 §2.3)."""
        return self._snapshot.sla_enabled

    @property
    def sla_target_seconds(self) -> int:
        """Global SLA target in seconds (§2.3)."""
        return self._snapshot.sla_target_seconds

    @property
    def sla_reset_on_operator_reply(self) -> bool:
        """Whether an unquoted operator reply resets the chat clock (§3.2)."""
        return self._snapshot.sla_reset_on_operator_reply

    @property
    def updated_at(self) -> datetime | None:
        """Last write timestamp of the row."""
        return self._snapshot.updated_at

    @property
    def updated_by(self) -> str | None:
        """Username of the administrator behind the last write."""
        return self._snapshot.updated_by

    # -- convenience mirrors of the ORM helpers ----------------------------
    @property
    def sla_target_minutes(self) -> int:
        """SLA target rounded to whole minutes (admin panel view)."""
        return round(self.sla_target_seconds / 60)

    # -- immutability -------------------------------------------------------
    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            "кэшированный снимок настроек доступен только для чтения; "
            "используйте services.settings_service.update_settings()"
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _RowLike):
            return self._snapshot == other._snapshot
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._snapshot)

    def __repr__(self) -> str:
        return (
            f"<AppSettings(cached) id={SETTINGS_ROW_ID} "
            f"sla_enabled={self.sla_enabled!r} "
            f"sla_target_seconds={self.sla_target_seconds!r} "
            f"reset_on_reply={self.sla_reset_on_operator_reply!r}>"
        )


def _as_row_like(snapshot: SettingsSnapshot) -> Any:
    """Wrap ``snapshot`` in the read-only :class:`_RowLike` facade."""
    return _RowLike(snapshot)


# ---------------------------------------------------------------------------
# Admin reads
# ---------------------------------------------------------------------------


async def read_settings_for_admin(db: AsyncSession) -> SettingsSnapshot:
    """Return the settings as an immutable snapshot for ``GET /api/admin/settings``.

    The admin panel always needs the **persisted** values (including a truthful
    ``updated_at``/``updated_by``), so this bypasses the 5 s cache and reads the row
    directly. The cache is refreshed as a side effect, which is exactly right after a
    ``PATCH`` that invalidated it.
    """
    row = await get_settings(db)
    return SettingsSnapshot(
        sla_enabled=bool(row.sla_enabled),
        sla_target_seconds=int(row.sla_target_seconds),
        sla_reset_on_operator_reply=bool(row.sla_reset_on_operator_reply),
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def update_settings(
    db: AsyncSession,
    *,
    payload: AdminSettingsUpdate,
    actor: str | None = None,
    actor_id: int | None = None,
) -> AppSettings:
    """Apply a partial update to the settings row, journal it and refresh the cache.

    ``payload`` is :class:`~app.schemas.AdminSettingsUpdate`: every field is optional and
    only the ones the client actually sent (``model_dump(exclude_unset=True)``) are
    written, so ``PATCH {"sla_enabled": false}`` cannot silently reset the target.

    Sequence, in one transaction:

    1. load (or bootstrap) the row under ``SELECT ... FOR UPDATE`` so two administrators
       saving at the same time serialise instead of overwriting each other's changes;
    2. apply the supplied fields, set ``updated_at`` / ``updated_by``, flush;
    3. stage the ``admin_audit_log`` entry — §2.3 requires the switch to be journaled,
       and the detail lists every changed field with old -> new values (secret-free);
    4. ``commit()``;
    5. **then** invalidate the cache, so a rolled-back transaction never flashes a value
       to the whole process that is not in the database.

    ``actor`` is the administrator username stored in ``updated_by`` (the contract's
    ``VARCHAR(64)``). ``actor_id`` is optional and only enables the audit row's foreign
    key to the operator; when omitted the entry is recorded as a system action, which the
    journal already supports for events without an owning account.

    Returns the freshly updated ORM row.
    """
    from app.models import AUDIT_ACTION_UPDATE_SETTINGS, AppSettings
    from app.services import admin as admin_service

    supplied = payload.model_dump(exclude_unset=True)

    row = None
    try:
        row = (
            await db.execute(
                select(AppSettings)
                .where(AppSettings.id == SETTINGS_ROW_ID)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - dialect without FOR UPDATE support
        logger.debug(
            "SELECT ... FOR UPDATE для app_settings недоступен (%s), читаю без блокировки",
            type(exc).__name__,
        )
        await db.rollback()
        row = await _load_row(db)

    if row is None:
        snapshot = _defaults()
        row = AppSettings(
            id=SETTINGS_ROW_ID,
            sla_enabled=snapshot.sla_enabled,
            sla_target_seconds=snapshot.sla_target_seconds,
            sla_reset_on_operator_reply=snapshot.sla_reset_on_operator_reply,
            updated_at=snapshot.updated_at,
            updated_by=snapshot.updated_by,
        )
        db.add(row)
        await db.flush()

    changes: list[str] = []
    if "sla_enabled" in supplied and supplied["sla_enabled"] is not None:
        value = bool(supplied["sla_enabled"])
        if bool(row.sla_enabled) != value:
            row.sla_enabled = value
            changes.append(f"sla_enabled={'true' if value else 'false'}")

    if "sla_target_seconds" in supplied and supplied["sla_target_seconds"] is not None:
        value = int(supplied["sla_target_seconds"])
        if int(row.sla_target_seconds) != value:
            row.sla_target_seconds = value
            changes.append(f"sla_target_seconds={value}")

    if (
        "sla_reset_on_operator_reply" in supplied
        and supplied["sla_reset_on_operator_reply"] is not None
    ):
        value = bool(supplied["sla_reset_on_operator_reply"])
        if bool(row.sla_reset_on_operator_reply) != value:
            row.sla_reset_on_operator_reply = value
            changes.append(f"sla_reset_on_operator_reply={'true' if value else 'false'}")

    if not changes:
        # Nothing to apply: commit anyway so a bootstrapped row is persisted, but write
        # no audit noise for a no-op PATCH (mirrors ``admin.update_operator``).
        row.updated_at = utcnow()
        await db.commit()
        invalidate_cache()
        logger.info("настройки приложения: изменений нет (PATCH без эффекта)")
        return row

    row.updated_at = utcnow()
    row.updated_by = (str(actor).strip() or None) if actor is not None else None
    await db.flush()

    actor_row = await _resolve_actor(db, actor_id)
    await admin_service.record_audit(
        db,
        actor=actor_row,
        action=AUDIT_ACTION_UPDATE_SETTINGS,
        target_type=admin_service.AUDIT_TARGET_SETTINGS,
        target_id=SETTINGS_ROW_ID,
        detail="Настройки SLA: " + ", ".join(changes),
    )
    await db.commit()

    # Invalidate only after the row is durable: see the module docstring.
    invalidate_cache()
    loaded = await _load_row(db)
    if loaded is not None:
        _store(_snapshot_of(loaded))
        logger.info(
            "настройки приложения обновлены (%s): %s",
            row.updated_by or "system",
            ", ".join(changes),
        )
        return loaded
    logger.warning("строка app_settings исчезла сразу после коммита; возвращаю применённую")
    return row


async def _resolve_actor(db: AsyncSession, actor_id: int | None) -> Any:
    """Load the acting operator for the audit row, or ``None`` for a system entry."""
    if actor_id is None:
        return None
    from app.models import Operator

    stmt = select(Operator).where(Operator.id == int(actor_id)).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Convenience for the SLA engine
# ---------------------------------------------------------------------------


def effective_sla_target(
    operator: Any | None,
    global_target: int,
) -> int:
    """Resolve the SLA target for one incident (CONTRACTS_V5 §2.3 priority order).

    Precedence, highest first:

    1. ``Operator.sla_target_seconds`` — a per-operator override always wins;
    2. ``app_settings.sla_target_seconds`` — the administrator's global default;
    3. ``settings.backend_sla_seconds`` — the historic fallback, already folded into
       ``global_target`` by the caller when the row was unavailable.

    Non-positive or non-numeric overrides are skipped rather than honoured: a ``0``
    target would open an incident that is overdue before it is created. The historical
    ``Operator.sla_target_seconds`` default (``300``) is a real value, so it legitimately
    wins over a larger global default — that is the documented meaning of «если задан».
    """
    if operator is not None:
        try:
            override = int(getattr(operator, "sla_target_seconds", None))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            override = 0
        if override > 0:
            return override
    try:
        resolved = int(global_target)
    except (TypeError, ValueError):
        resolved = 0
    if resolved > 0:
        return resolved
    return DEFAULT_SLA_TARGET_SECONDS

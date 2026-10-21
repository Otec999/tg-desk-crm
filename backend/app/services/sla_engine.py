"""SLA engine: incident lifecycle, round-robin assignment, live tick and statistics.

Frozen inputs (CONTRACTS.md):
    * §2 enums — ``SLAIncidentStatus`` PENDING/RESOLVED, ``ShiftStatus`` ONLINE/PAUSE/OFFLINE,
      ``MessageSenderType`` OPERATOR/CLIENT/BOT/SYSTEM;
    * §3.2 ``PersonalSlaStats``; §3.3 the embedded ``sla`` snapshot;
    * §4 ``SLA_UPDATE`` payload — ``chat_id``, ``incident_id``, ``status``, ``deadline_at``,
      ``remaining_seconds``, ``assigned_operator_id``, ``assigned_operator_name``, ``is_overdue``;
    * §6 tables ``group_chats`` / ``group_messages`` / ``sla_incidents``.

Rules enforced here:
    * Operators never open SLA incidents — their messages are answers, not questions.
    * At most **one** PENDING incident per chat: a second inbound message refreshes the existing
      clock instead of forking a duplicate one.
    * A reply resolves exactly one incident (the quoted one); siblings keep running.
    * Only ONLINE *and* WebSocket-connected operators are assignment candidates. The least loaded
      wins; ties are broken by a rotating cursor over the sorted operator ids.
    * :func:`tick` owns its session and commit, and can never raise, so the background ticker
      survives any database or broadcast failure.

CONTRACTS_V5 additions:
    * §2.3 — ``app_settings.sla_enabled = false`` makes :func:`open_incident` return ``None``
      for every message; already-open incidents keep running (nothing is auto-closed here).
    * §2.3 — the target seconds follow one priority chain everywhere
      (``Operator.sla_target_seconds`` -> ``app_settings.sla_target_seconds`` ->
      ``settings.backend_sla_seconds``), see :func:`_sla_target_for`.
    * §3.2 — ``app_settings.sla_reset_on_operator_reply`` decides whether an unquoted operator
      reply closes the chat's most urgent clock or nothing at all (legacy behaviour).

Transaction boundary: public coroutines only ``flush()``; callers ``commit()``. The exceptions are
:func:`tick` and the worker-facing ``session_factory=`` form of
:func:`open_incident` / :func:`resolve_for_reply`, which own their session and commit.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Final, Protocol

from sqlalchemy import and_, case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    GroupChat,
    GroupMessage,
    MessageSenderType,
    Operator,
    ShiftStatus,
    SLAIncident,
    SlaIncidentStatus,
)
from app.schemas import WsChatAssignedData, WsSlaUpdateData, dump_model
from app.utils import coerce_aware_utc, iso_z, utcnow
from app.ws_manager import EVT_CHAT_ASSIGNED, EVT_SLA_RESOLVED, EVT_SLA_UPDATE, envelope, manager

logger = logging.getLogger(__name__)

__all__ = [
    "BURNING_THRESHOLD_SECONDS",
    "DEFAULT_WINDOW_DAYS",
    "LOWEST_ID_LEAST_LOADED",
    "SLA_RESOLVED_LEAST_LOADED",
    "TICK_INCIDENT_LIMIT",
    "assign_round_robin",
    "broadcast_incident_resolved",
    "burning_chat_ids",
    "chat_assigned_payload",
    "close_incident",
    "has_pending_incident",
    "is_overdue",
    "least_loaded_strategy",
    "open_incident",
    "personal_stats",
    "remaining_seconds",
    "resolve_for_reply",
    "response_seconds",
    "sla_percent",
    "sla_summary",
    "sla_update_payload",
    "supervisor_dashboard",
    "tick",
    "tick_incidents",
]

#: ``burning`` chat filter threshold: PENDING incidents with 60 s or less left (CONTRACTS.md §3.3).
BURNING_THRESHOLD_SECONDS: Final[int] = 60

#: Guard rail for a single :func:`tick` pass (page size from the service contract).
TICK_INCIDENT_LIMIT: Final[int] = 500

#: Default statistical window for :func:`personal_stats` (CONTRACTS.md §3.2).
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: Broadcaster shape: ``await broadcast(envelope) -> int`` (matches ``manager.broadcast``).
BroadcastFn = Callable[..., Awaitable[Any]]

#: Tie-break strategies for :func:`assign_round_robin`.
SLA_RESOLVED_LEAST_LOADED: Final[str] = "sla_resolved_least_loaded"
LOWEST_ID_LEAST_LOADED: Final[str] = "lowest_id_least_loaded"


class _SessionFactoryLike(Protocol):
    """Anything callable without arguments that returns an ``AsyncSession``."""

    def __call__(self) -> Any: ...


# ============================================================================ pure helpers
def response_seconds(start: datetime | None, end: datetime | None) -> int:
    """Whole seconds between ``start`` and ``end``, clamped to ``>= 0``.

    Both sides are normalised to aware UTC first, so a naive value coming back from a test fake
    or a differently configured clock can never raise ``TypeError`` nor yield a negative response
    time. A missing timestamp yields ``0``.
    """
    if start is None or end is None:
        return 0
    delta = (coerce_aware_utc(end) - coerce_aware_utc(start)).total_seconds()
    if delta <= 0.0:
        return 0
    return int(delta)


def sla_percent(resolved: int, on_time: int) -> float:
    """``on_time / resolved`` as a percentage rounded half-up to one decimal place.

    * ``resolved <= 0`` -> ``100.0``: no data means no miss (never ``0.0``, never ``NaN``);
    * ``on_time`` is clamped into ``[0, resolved]`` so a bad counter cannot exceed 100 %;
    * rounding uses exact rational arithmetic (:class:`~fractions.Fraction`), not float ties, so
      ``99.95`` -> ``100.0`` and ``2/3`` -> ``66.7`` deterministically on every platform.
    """
    total = int(resolved)
    if total <= 0:
        return 100.0
    hits = min(max(int(on_time), 0), total)
    exact = Fraction(hits * 1000, total)  # tenths of a percent
    tenths = int((exact + Fraction(1, 2)).__floor__())
    return min(tenths, 1000) / 10.0


def remaining_seconds(deadline: datetime | None, now: datetime | None = None) -> int:
    """Seconds left until ``deadline`` (negative once overdue); clock-skew safe.

    ``math.floor`` keeps the client countdown from "jumping" a second early, and the result is
    always an ``int`` so the JSON payload matches the schema exactly.
    """
    if deadline is None:
        return 0
    reference = coerce_aware_utc(now) if now is not None else datetime.now(UTC)
    return math.floor((coerce_aware_utc(deadline) - reference).total_seconds())


def is_overdue(deadline: datetime | None, now: datetime | None = None) -> bool:
    """``True`` when ``deadline`` has already passed."""
    return remaining_seconds(deadline, now) < 0


def _is_burning(deadline: datetime | None, now: datetime | None = None) -> bool:
    """``True`` when the incident has ``<= 60`` s left or is already overdue."""
    return remaining_seconds(deadline, now) <= BURNING_THRESHOLD_SECONDS


def _status_value(value: Any) -> str:
    """Plain string of an enum-typed column value (``ShiftStatus.ONLINE`` -> ``"ONLINE"``)."""
    return str(getattr(value, "value", value) or "")


def _next_multiple_of_tick(delta: timedelta) -> timedelta:
    """Round ``delta`` up to the next multiple of ``BACKEND_WS_TICK_SECONDS``.

    Countdowns use :func:`math.floor`, so an incident opened at ``12:00:00.4`` with a 300 s
    target must land exactly on ``12:05:00`` — otherwise the client would render ``119`` where
    CONTRACTS.md §3.3 shows ``120``. The tick is the natural quantum of the countdown.
    """
    tick = int(getattr(settings, "backend_ws_tick_seconds", 2) or 2)
    total = delta.total_seconds()
    if total <= 0:
        # A non-positive window is meaningless for an SLA: never hand back a negative deadline.
        return timedelta(0)
    if tick <= 1:
        return delta
    steps = math.ceil(total / tick)
    return timedelta(seconds=steps * tick)


def _sla_target_for(operator: Operator | None, default_seconds: int | None = None) -> int:
    """Effective SLA target in seconds for one incident (always positive).

    Priority chain (CONTRACTS_V5 §2.3), highest first:

    1. ``operators.sla_target_seconds`` — the per-operator override, when it is set;
    2. ``app_settings.sla_target_seconds`` — the administrator's global default, passed in
       as ``default_seconds`` by the caller (this function is synchronous and never reads
       the database itself: the caller already awaited :func:`get_settings` and forwards
       the value, so an SLA target cannot cost an extra round-trip per message);
    3. ``settings.backend_sla_seconds`` — the historic pre-V5 fallback, used when no
       global default was supplied (tests, degraded read, direct callers);
    4. ``300`` — the hard floor if even the configuration is broken.

    ``default_seconds=None`` therefore reproduces the exact pre-V5 behaviour, which is
    what keeps the existing unit tests meaningful.
    """
    for candidate in (
        getattr(operator, "sla_target_seconds", None),
        default_seconds,
        getattr(settings, "backend_sla_seconds", None),
    ):
        try:
            value = int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 300


# ============================================================================ session plumbing
@asynccontextmanager
async def _session(
    db: AsyncSession | None,
    session_factory: _SessionFactoryLike | None,
) -> AsyncIterator[tuple[AsyncSession, bool]]:
    """Yield ``(session, owns_session)`` for the optional-session public API.

    When the caller supplied ``db`` this is a transparent pass-through and the caller keeps full
    control of the transaction boundary (services only ``flush()``). Otherwise a session is
    created from ``session_factory`` (or ``app.database.SessionLocal``), committed on success and
    rolled back on any exception — the form used by the Redis worker handlers.
    """
    if db is not None:
        yield db, False
        return

    factory = session_factory if session_factory is not None else _default_session_factory()
    session: AsyncSession = factory()
    try:
        yield session, True
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


def _default_session_factory() -> _SessionFactoryLike:
    """Lazily resolve ``app.database.SessionLocal`` (keeps this module DB-import free)."""
    from app.database import SessionLocal

    return SessionLocal


async def _load_chat(db: AsyncSession, chat_id: int, *, lock: bool = False) -> GroupChat | None:
    """Load a chat by primary key, optionally serialising concurrent assignment.

    ``lock=True`` issues ``SELECT ... FOR UPDATE SKIP LOCKED`` on PostgreSQL so two workers racing
    on the same chat never both assign it: the loser skips the row instead of blocking. The clause
    is omitted for other dialects (SQLite in unit tests) where it is a syntax error.
    """
    stmt = select(GroupChat).where(GroupChat.id == int(chat_id))
    if lock and _for_update_supported():
        stmt = stmt.with_for_update(of=GroupChat, skip_locked=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _load_incident(db: AsyncSession, incident_id: int) -> SLAIncident | None:
    """Load an incident by primary key."""
    stmt = select(SLAIncident).where(SLAIncident.id == int(incident_id))
    return (await db.execute(stmt)).scalar_one_or_none()


async def _load_operator(db: AsyncSession, operator_id: int | None) -> Operator | None:
    """Load an operator by primary key (``None`` for ``None``/unknown ids)."""
    if operator_id is None:
        return None
    stmt = select(Operator).where(Operator.id == int(operator_id))
    return (await db.execute(stmt)).scalar_one_or_none()


async def _operator_names(db: AsyncSession, operator_ids: Iterable[int | None]) -> dict[int, str]:
    """Resolve ``{operator_id: display_name}`` in a single round-trip."""
    wanted = {int(value) for value in operator_ids if value is not None}
    if not wanted:
        return {}
    rows = (
        await db.execute(select(Operator.id, Operator.display_name).where(Operator.id.in_(wanted)))
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


_FOR_UPDATE_SUPPORTED: bool | None = None


def _for_update_supported() -> bool:
    """``True`` when the configured dialect understands ``FOR UPDATE ... SKIP LOCKED``."""
    global _FOR_UPDATE_SUPPORTED
    if _FOR_UPDATE_SUPPORTED is None:
        try:
            from app.database import engine

            name = str(getattr(getattr(engine, "dialect", None), "name", "")).lower()
            _FOR_UPDATE_SUPPORTED = name in {"postgresql", "postgres"}
        except Exception:  # noqa: BLE001 - no engine/driver: stay on the portable path
            _FOR_UPDATE_SUPPORTED = False
    return _FOR_UPDATE_SUPPORTED


# ============================================================================ settings bridge
async def _load_app_settings(db: AsyncSession) -> Any | None:
    """Read the editable ``app_settings`` row, or ``None`` when it cannot be resolved.

    Never raises: the SLA engine is on the ingest hot path and a settings-table problem
    (migration not applied, transient DB error) must degrade to the historic behaviour
    instead of dropping an inbound message on the floor. Every failure is logged once per
    regression at ``WARNING`` so the problem stays visible.

    The import is local on purpose: ``app.services.settings_service`` imports the ORM
    models, and keeping that edge out of module scope means a partially written
    ``models.py`` cannot break ``sla_engine`` at import time.
    """
    try:
        from app.services import settings_service

        return await settings_service.get_settings(db)
    except Exception as exc:  # noqa: BLE001 - settings must never break the SLA engine
        logger.warning(
            "не удалось прочитать app_settings (%s: %s); используются настройки по умолчанию",
            type(exc).__name__,
            exc,
        )
        return None


def _sla_enabled(app_settings: Any | None) -> bool:
    """``True`` unless an administrator explicitly switched SLA off (§2.3).

    An unavailable row means "enabled": the pre-V5 behaviour, so a degraded read never
    silently stops every SLA clock in the system.
    """
    if app_settings is None:
        return True
    return bool(getattr(app_settings, "sla_enabled", True))


def _global_sla_target(app_settings: Any | None) -> int | None:
    """``app_settings.sla_target_seconds`` if usable, else ``None`` (fall back to config)."""
    if app_settings is None:
        return None
    try:
        value = int(getattr(app_settings, "sla_target_seconds", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _reset_on_operator_reply(app_settings: Any | None) -> bool:
    """``True`` when an unquoted operator reply should close the chat's clock (§3.2)."""
    if app_settings is None:
        return True
    return bool(getattr(app_settings, "sla_reset_on_operator_reply", True))


# ============================================================================ incident lifecycle
async def open_incident(
    db: AsyncSession | None,
    message: GroupMessage,
    *,
    session_factory: _SessionFactoryLike | None = None,
    broadcast: BroadcastFn | None = None,
) -> SLAIncident | None:
    """Open (or reuse) the SLA clock triggered by the inbound ``message``.

    Returns the incident that is now PENDING for the chat, or ``None`` when no clock applies:
        * an administrator switched SLA off (``app_settings.sla_enabled = false``,
          CONTRACTS_V5 §2.3) — no incident is ever created for this message;
        * operator-authored messages never open one (they answer);
        * if the chat already has a PENDING incident it is **reused** — the existing deadline is
          kept (a follow-up message must not extend the merchant's clock) and the incoming message
          is back-linked to it, so duplicates can never pile up;
        * otherwise a new ``SLAIncident`` is inserted, back-linked and assigned round-robin.

    The deadline uses the §2.3 priority chain: ``Operator.sla_target_seconds`` when set,
    else ``app_settings.sla_target_seconds``, else ``settings.backend_sla_seconds``.

    Only ``flush()`` is issued when ``db`` is supplied. Pass ``session_factory`` instead (with
    ``db=None``) to let this coroutine own the transaction and commit. ``broadcast`` is accepted
    for signature symmetry with the worker handlers; envelopes are emitted by the caller.
    """
    if _status_value(getattr(message, "sender_type", "")) == MessageSenderType.OPERATOR.value:
        logger.debug(
            "open_incident: message %s is OPERATOR-authored, no SLA clock opened",
            getattr(message, "id", "?"),
        )
        return None

    chat_id = int(getattr(message, "chat_id"))
    async with _session(db, session_factory) as (session, _owns):
        # §2.3: the master switch is checked before any row is touched. Existing incidents
        # keep running (they are never auto-closed here) — only *new* clocks are refused.
        app_settings = await _load_app_settings(session)
        if not _sla_enabled(app_settings):
            logger.debug(
                "open_incident: SLA выключен администратором, сообщение %s чата %s не взводит клок",
                getattr(message, "id", "?"),
                chat_id,
            )
            return None

        pending = await _pending_incident(session, chat_id)
        if pending is not None:
            logger.debug(
                "open_incident: chat %s already has PENDING incident %s (message %s absorbed)",
                chat_id,
                pending.id,
                getattr(message, "id", "?"),
            )
            message.sla_incident_id = pending.id
            await session.flush()
            return pending

        chat = await _load_chat(session, chat_id, lock=True)
        if chat is None:
            logger.warning(
                "open_incident: chat %s does not exist, no incident created", chat_id
            )
            return None

        owner = await _load_operator(session, getattr(chat, "assigned_operator_id", None))
        now = utcnow()
        target_seconds = _sla_target_for(owner, _global_sla_target(app_settings))
        deadline = now + _next_multiple_of_tick(timedelta(seconds=target_seconds))

        incident = SLAIncident(
            chat_id=chat_id,
            message_id=int(getattr(message, "id")),
            assigned_operator_id=getattr(chat, "assigned_operator_id", None),
            status=SlaIncidentStatus.PENDING.value,
            deadline_at=deadline,
            resolved_at=None,
            resolved_by_message_id=None,
            response_time_seconds=None,
            escalated=False,
            created_at=now,
        )
        session.add(incident)
        await session.flush()

        message.sla_incident_id = incident.id
        chosen = await assign_round_robin(session, chat, incident=incident)
        await session.flush()

        logger.info(
            "SLA incident %s opened for chat %s (message %s, target %ss, deadline %s, operator %s)",
            incident.id,
            chat_id,
            incident.message_id,
            target_seconds,
            iso_z(deadline),
            getattr(chosen, "id", None),
        )
        return incident


async def _pending_incident(db: AsyncSession, chat_id: int) -> SLAIncident | None:
    """The (single) PENDING incident of ``chat_id``, most urgent first.

    ``populate_existing`` forces a database read even when the incident is already in the
    identity map. Without it, an incident resolved earlier in the *same* session (whose guarded
    ``UPDATE`` bypasses the ORM) would still look PENDING in memory and a new inbound message
    would be silently absorbed instead of opening a fresh clock.
    """
    stmt = (
        select(SLAIncident)
        .where(
            SLAIncident.chat_id == int(chat_id),
            SLAIncident.status == SlaIncidentStatus.PENDING.value,
        )
        .order_by(SLAIncident.deadline_at.asc(), SLAIncident.id.asc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def has_pending_incident(db: AsyncSession, chat_id: int) -> bool:
    """``True`` when ``chat_id`` still has an open SLA clock (single ``EXISTS`` query)."""
    stmt = select(
        exists().where(
            and_(
                SLAIncident.chat_id == int(chat_id),
                SLAIncident.status == SlaIncidentStatus.PENDING.value,
            )
        )
    )
    return bool((await db.execute(stmt)).scalar_one_or_none())


async def resolve_for_reply(
    db: AsyncSession | None,
    chat_id: int,
    reply_to_msg_id: int | None,
    operator: Operator | None,
    outbound_message: GroupMessage | None,
    *,
    session_factory: _SessionFactoryLike | None = None,
    broadcast: BroadcastFn | None = None,
) -> SLAIncident | None:
    """Resolve the incident quoted by an operator reply.

    ``reply_to_msg_id`` is matched against ``GroupMessage.id`` (CRM id) **first**, then against
    ``GroupMessage.tg_msg_id`` (Telegram id) — same chat only, which is what CONTRACTS.md §3.4
    promises for ``POST /api/chats/{chat_id}/messages``.

    When the operator replies **without quoting** (``reply_to_msg_id is None``), the chat's most
    urgent open incident is resolved instead — but **only if**
    ``app_settings.sla_reset_on_operator_reply`` is ``True`` (the default, CONTRACTS_V5 §3.2).
    With the setting off, the engine keeps the legacy behaviour: an unquoted message closes
    nothing, and only a reply that quotes a customer message resolves its clock. The incident
    quoted by a reply always wins over that fallback.

    Only the quoted (or single most urgent) incident is resolved; siblings keep running. The state
    change is a guarded ``UPDATE ... WHERE status = 'PENDING'`` so two operators answering
    simultaneously can never double-count a response time (the loser sees ``rowcount == 0`` and
    gets ``None``). Returns the freshly reloaded (RESOLVED) incident, or ``None`` when nothing was
    resolved.

    Only ``flush()`` is issued when ``db`` is supplied; pass ``session_factory`` to own the commit.
    ``broadcast`` is emitted by the caller (``SLA_RESOLVED`` via
    :func:`broadcast_incident_resolved`).
    """
    if outbound_message is None:
        return None

    async with _session(db, session_factory) as (session, _owns):
        if reply_to_msg_id is None:
            # Unquoted answer. §3.2: only the (default) V5 rule makes this acknowledge the
            # chat's open clock; with the rule off we preserve the legacy semantics and
            # resolve nothing, so a stray message cannot mask a real SLA miss.
            app_settings = await _load_app_settings(session)
            if not _reset_on_operator_reply(app_settings):
                logger.debug(
                    "resolve_for_reply: chat %s — нецитатный ответ не сбрасывает SLA "
                    "(sla_reset_on_operator_reply=false)",
                    chat_id,
                )
                return None
            incident = await _pending_incident(session, chat_id)
            if incident is None:
                logger.debug(
                    "resolve_for_reply: chat %s has no open incident for an unquoted reply",
                    chat_id,
                )
                return None
        else:
            referenced = await _find_message(session, chat_id, reply_to_msg_id)
            if referenced is None:
                logger.debug(
                    "resolve_for_reply: reply target %s not found in chat %s",
                    reply_to_msg_id,
                    chat_id,
                )
                return None

            incident = await _incident_for_message(session, chat_id, referenced)
            if incident is None:
                return None

        now = utcnow()
        elapsed = response_seconds(incident.created_at, now)
        result = await session.execute(
            update(SLAIncident)
            .where(
                SLAIncident.id == int(incident.id),
                SLAIncident.status == SlaIncidentStatus.PENDING.value,
            )
            .values(
                status=SlaIncidentStatus.RESOLVED.value,
                resolved_at=now,
                resolved_by_message_id=int(getattr(outbound_message, "id")),
                response_time_seconds=elapsed,
            )
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            logger.info(
                "resolve_for_reply: incident %s was already resolved by another operator",
                incident.id,
            )
            return None

        outbound_message.sla_incident_id = incident.id
        await session.flush()

        # The guarded UPDATE bypassed the ORM, so the identity-map copy still holds the old
        # PENDING values. ``populate_existing`` forces the database state onto that instance —
        # without it the caller would broadcast a stale SLA_RESOLVED payload.
        resolved = (
            await session.execute(
                select(SLAIncident)
                .where(SLAIncident.id == int(incident.id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        logger.info(
            "SLA incident %s resolved by operator %s in chat %s (%ss)",
            incident.id,
            getattr(operator, "id", None),
            chat_id,
            elapsed,
        )
        return resolved if resolved is not None else incident


async def _find_message(db: AsyncSession, chat_id: int, identifier: int) -> GroupMessage | None:
    """Locate the quoted message by CRM id first, then by Telegram id (same chat only)."""
    value = int(identifier)
    by_id = (
        await db.execute(
            select(GroupMessage).where(
                GroupMessage.id == value, GroupMessage.chat_id == int(chat_id)
            )
        )
    ).scalar_one_or_none()
    if by_id is not None:
        return by_id
    # A CRM id is monotonically increasing; a negative value can only be a TG chat id, never a
    # message id, so the fallback query is skipped for negatives.
    if value < 0:
        return None
    stmt = (
        select(GroupMessage)
        .where(GroupMessage.chat_id == int(chat_id), GroupMessage.tg_msg_id == value)
        .order_by(GroupMessage.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _incident_for_message(
    db: AsyncSession,
    chat_id: int,
    referenced: GroupMessage,
) -> SLAIncident | None:
    """The PENDING incident triggered by ``referenced`` (back-link first, then ``message_id``)."""
    linked_id = getattr(referenced, "sla_incident_id", None)
    if linked_id is not None:
        linked = await _load_incident(db, int(linked_id))
        if linked is not None and _status_value(linked.status) == SlaIncidentStatus.PENDING.value:
            return linked
    stmt = (
        select(SLAIncident)
        .where(
            SLAIncident.chat_id == int(chat_id),
            SLAIncident.message_id == int(referenced.id),
            SLAIncident.status == SlaIncidentStatus.PENDING.value,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def close_incident(
    db: AsyncSession | None,
    incident: SLAIncident,
    *,
    resolved_by_message: GroupMessage | None = None,
    resolved_at: datetime | None = None,
    session_factory: _SessionFactoryLike | None = None,
) -> SLAIncident:
    """Administrative close without a reply: mark RESOLVED and freeze the response time.

    Supervision tooling uses this; operator replies go through :func:`resolve_for_reply`. Only
    flushes when the caller owns the session.
    """
    async with _session(db, session_factory) as (session, _owns):
        now = resolved_at or utcnow()
        incident.status = SlaIncidentStatus.RESOLVED.value
        incident.resolved_at = now
        incident.response_time_seconds = response_seconds(incident.created_at, now)
        if resolved_by_message is not None:
            incident.resolved_by_message_id = int(resolved_by_message.id)
            resolved_by_message.sla_incident_id = incident.id
        await session.flush()
        return incident


# ============================================================================ assignment
#: Rotating cursor over sorted candidate ids — the round-robin half of the tie-break.
_ROUND_ROBIN_CURSOR = itertools.count()


def _rotate(candidates: list[int]) -> list[int]:
    """Rotate ``candidates`` by the shared monotonic round-robin cursor."""
    if len(candidates) <= 1:
        return list(candidates)
    offset = next(_ROUND_ROBIN_CURSOR) % len(candidates)
    return candidates[offset:] + candidates[:offset]


def _pick_least_loaded(
    candidates: Iterable[int],
    loads: Mapping[int, int],
    strategy: str = SLA_RESOLVED_LEAST_LOADED,
) -> int | None:
    """Pure selection: least loaded first, then the rotating round-robin order.

    Deterministic tie-break (``LOWEST_ID_LEAST_LOADED``) is available for tests and for callers
    that prefer a stable target over fairness.
    """
    ordered = _rotate(sorted({int(item) for item in candidates}))
    if not ordered:
        return None
    ranking = sorted(
        (loads.get(operator_id, 0), position, operator_id)
        for position, operator_id in enumerate(ordered)
    )
    best_load = ranking[0][0]
    tied = [operator_id for load, _position, operator_id in ranking if load == best_load]
    return min(tied) if strategy == LOWEST_ID_LEAST_LOADED else tied[0]


def least_loaded_strategy(status: str, operator_id: int) -> int:
    """Secondary counter used when every ONLINE operator has an empty queue.

    Fewer incidents already RESOLVED today is better, so the next chat goes to whoever is least
    served. ``operator_id`` breaks remaining ties deterministically.
    """
    if status == SlaIncidentStatus.RESOLVED.value:
        return int(operator_id or 0)
    return 0


async def _eligible_operators(db: AsyncSession) -> list[int]:
    """Ids of operators that are ONLINE **and** hold a live WebSocket, ascending."""
    stmt = (
        select(Operator.id)
        .where(
            Operator.shift_status == ShiftStatus.ONLINE.value,
            Operator.is_online_ws.is_(True),
        )
        .order_by(Operator.id.asc())
    )
    return [int(value) for value in (await db.execute(stmt)).scalars().all()]


async def _load_counts(db: AsyncSession, operator_ids: Iterable[int]) -> dict[int, int]:
    """PENDING incident count per operator — one aggregate query, not N+1."""
    wanted = [int(value) for value in operator_ids]
    if not wanted:
        return {}
    stmt = (
        select(SLAIncident.assigned_operator_id, func.count(SLAIncident.id))
        .where(
            SLAIncident.status == SlaIncidentStatus.PENDING.value,
            SLAIncident.assigned_operator_id.in_(wanted),
        )
        .group_by(SLAIncident.assigned_operator_id)
    )
    return {int(row[0]): int(row[1]) for row in (await db.execute(stmt)).all()}


async def _resolved_counts(db: AsyncSession, operator_ids: Iterable[int]) -> dict[int, int]:
    """RESOLVED incident count per operator today (rotating tie-break input)."""
    wanted = [int(value) for value in operator_ids]
    if not wanted:
        return {}
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(SLAIncident.assigned_operator_id, func.count(SLAIncident.id))
        .where(
            SLAIncident.status == SlaIncidentStatus.RESOLVED.value,
            SLAIncident.assigned_operator_id.in_(wanted),
            SLAIncident.resolved_at >= day_start,
        )
        .group_by(SLAIncident.assigned_operator_id)
    )
    return {int(row[0]): int(row[1]) for row in (await db.execute(stmt)).all()}


async def assign_round_robin(
    db: AsyncSession,
    chat: GroupChat,
    *,
    incident: SLAIncident | None = None,
) -> Operator | None:
    """Assign ``chat`` (and ``incident``) to the best ONLINE operator.

    * The current owner is kept while they are still ONLINE — reassignment would steal a chat from
      the operator actively working it.
    * Otherwise the least loaded ONLINE + WS-connected operator wins: primary key is the number
      of PENDING incidents, ties are broken by the rotating round-robin cursor, then by today's
      RESOLVED count, then by id.
    * Nobody ONLINE -> the incident stays unassigned and :func:`tick` retries next pass.

    Only ``flush()`` is issued; the caller commits.
    """
    candidates = await _eligible_operators(db)
    if not candidates:
        logger.debug("assign_round_robin: no ONLINE operator, chat %s left unassigned", chat.id)
        return None

    current_owner = getattr(chat, "assigned_operator_id", None)
    if current_owner is not None and int(current_owner) in candidates:
        owner = await _load_operator(db, int(current_owner))
        if owner is not None:
            _apply_assignment(chat, incident, owner)
            await db.flush()
            return owner

    loads = await _load_counts(db, candidates)
    incident_owner = getattr(incident, "assigned_operator_id", None) if incident is not None else None
    if incident_owner is not None and int(incident_owner) in loads:
        # The incident's own clock must not count against its prospective owner.
        loads[int(incident_owner)] = max(loads[int(incident_owner)] - 1, 0)

    least_load = min(loads.get(operator_id, 0) for operator_id in candidates)
    tied = [operator_id for operator_id in candidates if loads.get(operator_id, 0) == least_load]
    rotated = _rotate(sorted(tied))
    if len(rotated) == 1:
        chosen = rotated[0]
    else:
        # Secondary tie-break: whoever resolved the fewest incidents today goes next. The rotation
        # offset is folded into the sort key (negative load, then rotation position) so a single
        # pass picks the winner in O(n log n) without re-scanning the list.
        resolved_counts = await _resolved_counts(db, rotated)
        chosen = min(
            rotated,
            key=lambda operator_id: (
                resolved_counts.get(operator_id, 0),
                rotated.index(operator_id),
            ),
        )

    owner = await _load_operator(db, chosen)
    if owner is None:  # pragma: no cover - row deleted between the two queries
        return None
    _apply_assignment(chat, incident, owner)
    await db.flush()
    logger.info(
        "assign_round_robin: chat %s -> operator %s (%s); load before=%s",
        chat.id,
        owner.id,
        owner.display_name,
        loads.get(owner.id, 0),
    )
    return owner


def _apply_assignment(chat: GroupChat, incident: SLAIncident | None, owner: Operator) -> None:
    """Write the assignment onto the chat and, when given, onto the incident."""
    chat.assigned_operator_id = owner.id
    if incident is not None:
        incident.assigned_operator_id = owner.id


# ============================================================================ WS payloads
def sla_update_payload(
    incident: SLAIncident,
    *,
    chat: GroupChat | None = None,
    assigned_operator_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the exact ``SLA_UPDATE`` data object of CONTRACTS.md §4.

    ``remaining_seconds`` / ``is_overdue`` are recomputed server-side from ``deadline_at`` so a
    stale row can never freeze the client countdown.
    """
    reference = coerce_aware_utc(now) if now is not None else datetime.now(UTC)
    remaining = remaining_seconds(incident.deadline_at, reference)
    if assigned_operator_name is None:
        resolved_name = getattr(incident, "assigned_operator_name", None)
        if resolved_name is None and chat is not None:
            resolved_name = getattr(chat, "assigned_operator_name", None)
        assigned_operator_name = resolved_name
    payload = WsSlaUpdateData(
        chat_id=int(incident.chat_id if chat is None else chat.id),
        incident_id=int(incident.id),
        status=_status_value(incident.status),
        deadline_at=incident.deadline_at,
        remaining_seconds=remaining,
        assigned_operator_id=getattr(incident, "assigned_operator_id", None),
        assigned_operator_name=assigned_operator_name,
        is_overdue=remaining < 0,
    )
    return dump_model(payload)


def chat_assigned_payload(
    chat: GroupChat,
    operator: Operator | None = None,
    *,
    operator_name: str | None = None,
) -> dict[str, Any]:
    """Build the ``CHAT_ASSIGNED`` data object (CONTRACTS.md §4)."""
    name = operator_name
    if name is None and operator is not None:
        name = getattr(operator, "display_name", None)
    if name is None:
        name = getattr(chat, "assigned_operator_name", None)
    payload = WsChatAssignedData(
        chat_id=int(chat.id),
        assigned_operator_id=getattr(chat, "assigned_operator_id", None),
        assigned_operator_name=name,
    )
    return dump_model(payload)


async def broadcast_incident_resolved(
    broadcast: BroadcastFn | None,
    incident: SLAIncident,
    *,
    operator: Operator | None = None,
    stats: dict[str, Any] | None = None,
) -> bool:
    """Best-effort ``SLA_RESOLVED`` envelope for the routers (never raises).

    Data (CONTRACTS.md §4): ``{chat_id, incident_id, operator_id, response_time_seconds, stats}``
    with ``stats`` = :class:`~app.schemas.PersonalSlaStats` for the resolving operator, when the
    caller computed it.
    """
    target = broadcast if broadcast is not None else manager.broadcast
    try:
        await target(
            envelope(
                EVT_SLA_RESOLVED,
                {
                    "chat_id": int(incident.chat_id),
                    "incident_id": int(incident.id),
                    "operator_id": getattr(operator, "id", None),
                    "response_time_seconds": getattr(incident, "response_time_seconds", None),
                    "stats": stats,
                },
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001 - broadcasts are best effort
        logger.warning("SLA_RESOLVED broadcast failed: %s: %s", type(exc).__name__, exc)
        return False


# ============================================================================ tick
async def tick_incidents(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = TICK_INCIDENT_LIMIT,
) -> list[tuple[SLAIncident, GroupChat | None, bool]]:
    """Load and refresh every PENDING incident; return ``(incident, chat, escalated_now)``.

    * loads at most ``limit`` PENDING incidents joined with their chat, most urgent first;
    * sets ``escalated = True`` once the deadline passed and reports that *transition* only;
    * retries the round-robin assignment for incidents that still have no owner;
    * re-checks ``status`` after loading so an incident resolved by a concurrent reply during
      this pass is never re-broadcast.

    Only ``flush()`` is issued — :func:`tick` owns the commit.
    """
    reference = coerce_aware_utc(now) if now is not None else utcnow()
    stmt = (
        select(SLAIncident, GroupChat)
        .join(GroupChat, SLAIncident.chat_id == GroupChat.id)
        .where(SLAIncident.status == SlaIncidentStatus.PENDING.value)
        .order_by(SLAIncident.deadline_at.asc(), SLAIncident.id.asc())
        .limit(max(int(limit), 1))
    )
    rows = (await db.execute(stmt)).all()

    frames: list[tuple[SLAIncident, GroupChat | None, bool]] = []
    for incident, chat in rows:
        if _status_value(incident.status) != SlaIncidentStatus.PENDING.value:
            continue

        escalated_now = False
        if remaining_seconds(incident.deadline_at, reference) < 0 and not bool(incident.escalated):
            # A guarded UPDATE instead of a plain attribute write: the flag must survive the
            # commit regardless of flush ordering, otherwise every tick would repeat the
            # escalation frame for the same incident.
            await db.execute(
                update(SLAIncident)
                .where(SLAIncident.id == int(incident.id), SLAIncident.escalated.is_(False))
                .values(escalated=True)
                .execution_options(synchronize_session=False)
            )
            incident.escalated = True
            escalated_now = True
            logger.warning(
                "SLA incident %s (chat %s) is overdue and has been escalated",
                incident.id,
                incident.chat_id,
            )

        if getattr(incident, "assigned_operator_id", None) is None:
            try:
                await assign_round_robin(db, chat, incident=incident)
            except Exception as exc:  # noqa: BLE001 - one bad row must not abort the pass
                logger.warning(
                    "tick: assignment retry failed for incident %s: %s: %s",
                    incident.id,
                    type(exc).__name__,
                    exc,
                )

        frames.append((incident, chat, escalated_now))

    if rows:
        await db.flush()
    return frames


async def tick(
    session_factory: _SessionFactoryLike | None = None,
    broadcast: BroadcastFn | None = None,
    *,
    now: datetime | None = None,
    limit: int = TICK_INCIDENT_LIMIT,
) -> None:
    """One SLA heartbeat: escalate, (re)assign, then broadcast one ``SLA_UPDATE`` per incident.

    Contract of this coroutine:

    * **owns** its session and commits exactly once;
    * broadcasts exactly one ``SLA_UPDATE`` envelope per open incident per pass, with the §4
      payload shape;
    * on the pass where an incident first crosses its deadline, an overdue frame is emitted
      **before** the regular one (the client flashes the escalation immediately);
    * emits ``CHAT_ASSIGNED`` when a pending incident just got an owner;
    * **never raises** — every failure is logged and swallowed so the background ticker keeps
      its schedule.
    """
    target = broadcast if broadcast is not None else manager.broadcast
    frames: list[tuple[SLAIncident, GroupChat | None, bool]] = []
    names: dict[int, str] = {}

    session: AsyncSession | None = None
    try:
        factory = session_factory if session_factory is not None else _default_session_factory()
        session = factory()
        frames = await tick_incidents(session, now=now, limit=limit)
        await session.commit()
        # Names are resolved on the same session, after the commit: ``SessionLocal`` uses
        # ``expire_on_commit=False`` so the loaded rows stay usable here.
        names = await _collect_names(session, frames)
    except Exception as exc:  # noqa: BLE001 - the ticker must survive any database failure
        logger.exception("sla tick failed: %s: %s", type(exc).__name__, exc)
        if session is not None:
            try:
                await session.rollback()
            except Exception as rollback_exc:  # noqa: BLE001
                logger.debug("sla tick: rollback failed: %s", rollback_exc)
        return
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as close_exc:  # noqa: BLE001
                logger.debug("sla tick: session close failed: %s", close_exc)

    reference = coerce_aware_utc(now) if now is not None else datetime.now(UTC)
    for incident, chat, escalated_now in frames:
        operator_id = getattr(incident, "assigned_operator_id", None)
        payload = sla_update_payload(
            incident,
            chat=chat,
            assigned_operator_name=names.get(int(operator_id)) if operator_id else None,
            now=reference,
        )
        if escalated_now:
            escalated = {**payload, "is_overdue": True}
            await _safe_broadcast(target, EVT_SLA_UPDATE, escalated, incident.id)
        await _safe_broadcast(target, EVT_SLA_UPDATE, payload, incident.id)

        if chat is not None and operator_id is not None:
            await _safe_broadcast(
                target,
                EVT_CHAT_ASSIGNED,
                chat_assigned_payload(
                    chat,
                    operator_name=payload.get("assigned_operator_name"),
                ),
                incident.id,
            )


async def _collect_names(
    session: AsyncSession,
    frames: list[tuple[SLAIncident, GroupChat | None, bool]],
) -> dict[int, str]:
    """Resolve operator display names for ``frames``; failures degrade to an empty map."""
    try:
        return await _operator_names(session, _assigned_ids(frames))
    except Exception as exc:  # noqa: BLE001 - names are cosmetic
        logger.debug("sla tick: operator name lookup failed: %s: %s", type(exc).__name__, exc)
        return {}


def _assigned_ids(
    frames: Iterable[tuple[SLAIncident, GroupChat | None, bool]],
) -> set[int | None]:
    """Every operator id referenced by ``frames`` (incident owners and chat owners)."""
    ids: set[int | None] = set()
    for incident, chat, _escalated in frames:
        ids.add(getattr(incident, "assigned_operator_id", None))
        if chat is not None:
            ids.add(getattr(chat, "assigned_operator_id", None))
    return ids


async def _safe_broadcast(
    target: BroadcastFn,
    event_type: str,
    payload: dict[str, Any],
    incident_id: int,
) -> None:
    """Send one envelope, swallowing transport errors so one dead client cannot stop the tick."""
    try:
        await target(envelope(event_type, payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sla tick: broadcast of %s for incident %s failed: %s: %s",
            event_type,
            incident_id,
            type(exc).__name__,
            exc,
        )


# ============================================================================ read models
async def sla_summary(db: AsyncSession, chat_id: int) -> dict[str, Any] | None:
    """The ``sla`` object embedded in ``ChatListItem`` (CONTRACTS.md §3.3) — PENDING only.

    Shape: ``{status, deadline_at, remaining_seconds, is_overdue}`` plus the additive
    ``incident_id`` / ``assigned_operator_id`` / ``assigned_operator_name`` keys the frontend uses
    for the badge tooltip. ``None`` when the chat has no open clock.
    """
    incident = await _pending_incident(db, chat_id)
    if incident is None:
        return None
    owner_id = getattr(incident, "assigned_operator_id", None)
    name = (await _operator_names(db, [owner_id])).get(int(owner_id)) if owner_id is not None else None
    remaining = remaining_seconds(incident.deadline_at)
    return {
        "status": SlaIncidentStatus.PENDING.value,
        "deadline_at": iso_z(incident.deadline_at),
        "remaining_seconds": remaining,
        "is_overdue": remaining < 0,
        "incident_id": int(incident.id),
        "assigned_operator_id": owner_id,
        "assigned_operator_name": name,
    }


async def burning_chat_ids(
    db: AsyncSession,
    *,
    threshold_seconds: int = BURNING_THRESHOLD_SECONDS,
    now: datetime | None = None,
) -> set[int]:
    """Chats with a PENDING incident that has ``<= threshold`` seconds left, or is overdue.

    The comparison runs in PostgreSQL against a bound timestamp (single ``SELECT DISTINCT``), so
    clock handling stays server-side and no rows travel to Python.
    """
    reference = coerce_aware_utc(now) if now is not None else datetime.now(UTC)
    cutoff = reference + timedelta(seconds=max(int(threshold_seconds), 0))
    stmt = (
        select(SLAIncident.chat_id)
        .where(
            SLAIncident.status == SlaIncidentStatus.PENDING.value,
            SLAIncident.deadline_at <= cutoff,
        )
        .distinct()
    )
    return {int(value) for value in (await db.execute(stmt)).scalars().all()}


async def personal_stats(
    db: AsyncSession,
    operator: Operator,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """``<PersonalSlaStats>`` (CONTRACTS.md §3.2) for ``operator`` over the last ``window_days``.

    Two aggregate queries, no N+1:

    1. RESOLVED incidents assigned to the operator inside the window -> ``resolved_count``,
       ``avg_response_seconds``, and the on-time counter (``response_time_seconds <= target``);
    2. PENDING incidents assigned to the operator -> ``open_count`` and ``overdue_count``
       (``deadline_at < now``).

    A NULL ``response_time_seconds`` on an older row is derived from the timestamps rather than
    dropped, so seed/legacy data never under-reports the average.

    ``sla_target_seconds`` follows the same CONTRACTS_V5 §2.3 priority chain as
    :func:`open_incident` — per-operator override, then the global ``app_settings`` value,
    then the configuration fallback — so the on-time percentage a supervisor reads is
    computed against the very window the incidents were opened with.
    """
    operator_id = int(operator.id)
    app_settings = await _load_app_settings(db)
    target = _sla_target_for(operator, _global_sla_target(app_settings))
    reference = utcnow()
    window_start = reference - timedelta(days=max(int(window_days), 1))
    response_expr = func.coalesce(
        SLAIncident.response_time_seconds,
        func.extract("epoch", SLAIncident.resolved_at - SLAIncident.created_at),
    )

    resolved_stmt = select(
        func.count(SLAIncident.id),
        func.coalesce(func.sum(response_expr), 0),
        func.coalesce(
            func.sum(case((response_expr <= target, 1), else_=0)),
            0,
        ),
    ).where(
        SLAIncident.assigned_operator_id == operator_id,
        SLAIncident.status == SlaIncidentStatus.RESOLVED.value,
        SLAIncident.resolved_at.is_not(None),
        SLAIncident.resolved_at >= window_start,
    )
    resolved_count, total_seconds, on_time_count = (await db.execute(resolved_stmt)).one()

    open_stmt = select(
        func.count(SLAIncident.id),
        func.coalesce(
            func.sum(case((SLAIncident.deadline_at < reference, 1), else_=0)),
            0,
        ),
    ).where(
        SLAIncident.assigned_operator_id == operator_id,
        SLAIncident.status == SlaIncidentStatus.PENDING.value,
    )
    open_count, overdue_count = (await db.execute(open_stmt)).one()

    resolved_int = int(resolved_count or 0)
    average = int(round(float(total_seconds or 0) / resolved_int)) if resolved_int else 0
    return {
        "operator_id": operator_id,
        "shift_status": _status_value(getattr(operator, "shift_status", ShiftStatus.OFFLINE.value)),
        "avg_response_seconds": max(average, 0),
        "resolved_count": resolved_int,
        "open_count": max(int(open_count or 0), 0),
        "overdue_count": max(int(overdue_count or 0), 0),
        "sla_percent": sla_percent(resolved_int, int(on_time_count or 0)),
        "sla_target_seconds": target,
    }


async def supervisor_dashboard(db: AsyncSession) -> dict[str, Any]:
    """Aggregated supervision board: global totals plus one row per operator.

    Returns::

        {
          "generated_at": "2024-06-01T12:00:00Z",
          "total_open": 3,
          "total_overdue": 1,
          "total_pending_unassigned": 0,
          "resolved_today": 12,
          "avg_response_seconds": 74,
          "sla_percent": 96.0,
          "operators": [
            {"operator_id": 1, "username": "op1", "display_name": "Operator One",
             "shift_status": "ONLINE", "is_online_ws": true, "assigned_open": 2,
             "overdue": 0, "resolved_today": 5, "avg_response_seconds": 61,
             "sla_percent": 100.0, "sla_target_seconds": 300},
            ...
          ]
        }

    Everything comes from aggregate SQL (one grouped query over ``operators LEFT JOIN
    sla_incidents`` plus one pending-totals query), never from per-operator loops.

    ``sla_target_seconds`` per row follows the CONTRACTS_V5 §2.3 priority chain, so the
    reported target is the one the operator's incidents are actually clocked against.
    """
    reference = utcnow()
    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    app_settings = await _load_app_settings(db)
    global_target = _sla_target_for(None, _global_sla_target(app_settings))
    pending = SlaIncidentStatus.PENDING.value
    resolved_status = SlaIncidentStatus.RESOLVED.value
    response_expr = func.coalesce(
        SLAIncident.response_time_seconds,
        func.extract("epoch", SLAIncident.resolved_at - SLAIncident.created_at),
    )
    is_pending = SLAIncident.status == pending
    is_resolved_today = and_(
        SLAIncident.status == resolved_status,
        SLAIncident.resolved_at >= day_start,
    )

    per_operator_stmt = (
        select(
            Operator.id,
            Operator.username,
            Operator.display_name,
            Operator.shift_status,
            Operator.sla_target_seconds,
            Operator.is_online_ws,
            func.count(case((is_pending, SLAIncident.id))).label("assigned_open"),
            func.count(
                case((and_(is_pending, SLAIncident.deadline_at < reference), SLAIncident.id))
            ).label("overdue"),
            func.count(case((is_resolved_today, SLAIncident.id))).label("resolved_today"),
            func.coalesce(func.sum(case((is_resolved_today, response_expr))), 0).label(
                "total_response_seconds"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(is_resolved_today, response_expr <= Operator.sla_target_seconds),
                            1,
                        )
                    )
                ),
                0,
            ).label("on_time_today"),
        )
        .select_from(Operator)
        .outerjoin(SLAIncident, SLAIncident.assigned_operator_id == Operator.id)
        .group_by(
            Operator.id,
            Operator.username,
            Operator.display_name,
            Operator.shift_status,
            Operator.sla_target_seconds,
            Operator.is_online_ws,
        )
        .order_by(Operator.id.asc())
    )
    rows = (await db.execute(per_operator_stmt)).all()

    operators: list[dict[str, Any]] = []
    total_overdue = total_resolved = 0
    total_seconds = 0.0
    total_on_time = 0
    for row in rows:
        resolved_int = int(row.resolved_today or 0)
        average = int(round(float(row.total_response_seconds or 0) / resolved_int)) if resolved_int else 0
        operators.append(
            {
                "operator_id": int(row.id),
                "username": str(row.username),
                "display_name": str(row.display_name),
                "shift_status": _status_value(row.shift_status),
                "is_online_ws": bool(row.is_online_ws),
                "assigned_open": int(row.assigned_open or 0),
                "overdue": int(row.overdue or 0),
                "resolved_today": resolved_int,
                "avg_response_seconds": max(average, 0),
                "sla_percent": sla_percent(resolved_int, int(row.on_time_today or 0)),
                # §2.3: the per-operator override when set, else the global default that
                # was already resolved once above (never a second settings read per row).
                "sla_target_seconds": _sla_target_for(
                    SimpleNamespace(sla_target_seconds=row.sla_target_seconds),
                    global_target,
                ),
            }
        )
        total_overdue += int(row.overdue or 0)
        total_resolved += resolved_int
        total_seconds += float(row.total_response_seconds or 0)
        total_on_time += int(row.on_time_today or 0)

    totals_stmt = select(
        func.count(SLAIncident.id),
        func.coalesce(
            func.sum(case((SLAIncident.assigned_operator_id.is_(None), 1), else_=0)),
            0,
        ),
    ).where(is_pending)
    total_open, unassigned_open = (await db.execute(totals_stmt)).one()

    return {
        "generated_at": iso_z(reference),
        "total_open": int(total_open or 0),
        "total_overdue": total_overdue,
        "total_pending_unassigned": int(unassigned_open or 0),
        "resolved_today": total_resolved,
        "avg_response_seconds": int(round(total_seconds / total_resolved)) if total_resolved else 0,
        "sla_percent": sla_percent(total_resolved, total_on_time),
        "operators": operators,
    }

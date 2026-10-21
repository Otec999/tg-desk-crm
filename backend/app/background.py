"""Long-running background tasks: heartbeat sweeper, SLA ticker, Redis consumer.

All three are started in ``app.main.lifespan`` through :func:`start_all` and cancelled in
:func:`stop_all`. Each task is fully self-healing:

* ``heartbeat_sweeper`` never raises — DB hiccups are logged and retried on the next tick.
* ``sla_ticker`` probes the ``app.services.sla_engine.tick`` signature once and calls it
  correctly whether the service agent implemented ``tick(ctx)`` or ``tick(session_factory,
  broadcast)``. A missing module is retried later, so a partially deployed backend still boots.
* ``redis_consumer`` reconnects with 1s -> 30s exponential backoff, forever.

Import of ``app.models`` / ``app.schemas`` / ``app.services.*`` happens **inside** functions
so this module never creates an import cycle with the modules other agents own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.redis_bus import (
    CH_TG_DELETED,
    CH_TG_EDITS,
    CH_TG_INCOMING,
    CH_TG_OUTGOING_ACK,
    CH_TG_REACTIONS,
    CH_WS_FANOUT,
    publish,
    pubsub,
)
from app.ws_manager import (
    EVT_OPERATOR_PRESENCE,
    EVT_SHIFT_CHANGED,
    EVT_SLA_UPDATE,
    ConnectionManager,
    envelope,
    manager,
)

logger = logging.getLogger(__name__)

#: (envelope, exclude_operator_id) -> delivered frame count. Matches ``manager.broadcast``.
BroadcastFn = Callable[..., Awaitable[int]]

HEARTBEAT_INTERVAL_SECONDS: float = 5.0
TICKER_RETRY_SECONDS: float = 2.0
BACKOFF_INITIAL_SECONDS: float = 1.0
BACKOFF_MAX_SECONDS: float = 30.0

TASK_HEARTBEAT: str = "heartbeat_sweeper"
TASK_SLA: str = "sla_ticker"
TASK_REDIS: str = "redis_consumer"
TASK_NAMES: tuple[str, ...] = (TASK_HEARTBEAT, TASK_SLA, TASK_REDIS)

#: channel -> handler name in ``app.services.tg_ingest`` (CONTRACTS.md §5)
CHANNEL_HANDLERS: dict[str, str] = {
    CH_TG_INCOMING: "handle_incoming",
    CH_TG_DELETED: "handle_deleted",
    CH_TG_EDITS: "handle_edit",
    CH_TG_OUTGOING_ACK: "handle_outgoing_ack",
    CH_TG_REACTIONS: "handle_reactions",
}


class TaskCancelled(Exception):
    """Raised by :func:`sleep_or_cancel` when the owning task is being shut down."""


class _RetrySignal(RuntimeError):
    """Internal marker: a loop body asked to retry without logging a crash."""


# ================================================================= shared context / utilities
@dataclass(slots=True)
class BackgroundContext:
    """Everything the background workers need, without reaching for globals."""

    session_factory: async_sessionmaker[AsyncSession]
    broadcast: BroadcastFn
    manager: ConnectionManager = field(default_factory=lambda: manager)


_last_slept: asyncio.Event | None = None


def wait_slept() -> asyncio.Event:
    """Event set after every backoff sleep — lets async tests prove the backoff advanced."""
    global _last_slept
    if _last_slept is None:
        _last_slept = asyncio.Event()
    return _last_slept


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    """Datetime -> ``2024-06-01T12:00:00Z`` (naive datetimes are treated as UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _label(value: Any) -> str:
    """Normalise an enum member / plain string into its contract string value.

    Also recovers ``"<class 'app.enums.ShiftStatus'>"``-style reprs produced by fetching a
    missing attribute, so a peer's rename can never produce a garbage WS enum value.
    """
    raw = getattr(value, "value", value)
    if isinstance(raw, str):
        if raw.startswith("<class '"):
            return raw.rstrip(">").rsplit(".", 1)[-1].strip("'")
        return raw
    return str(raw).rsplit(".", 1)[-1]


def _enum_attr(module: Any, *names: str) -> Any:
    """Fetch an enum class from ``module``, tolerating spelling variants.

    The enums agent exposes ``SlaIncidentStatus`` while CONTRACTS.md writes
    ``SLAIncidentStatus``; both spellings are accepted.
    """
    for name in names:
        candidate = getattr(module, name, None)
        if candidate is not None:
            return candidate
    raise AttributeError(f"none of {names!r} found in {getattr(module, '__name__', module)!r}")


async def sleep_or_cancel(seconds: float, task_name: str) -> None:
    """Sleep, but wake immediately (raising :class:`TaskCancelled`) when shutdown begins."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError as exc:
        raise TaskCancelled(task_name) from exc


async def _supervised_loop(
    name: str,
    body: Callable[[], Awaitable[None]],
    restart_delay: float,
) -> None:
    """Run ``body`` forever, isolating every unexpected exception and honouring cancellation."""
    while True:
        try:
            await body()
        except TaskCancelled:
            logger.info("Task %s cancelled", name)
            return
        except asyncio.CancelledError:
            logger.info("Task %s cancelled", name)
            raise
        except _RetrySignal:
            logger.warning("Task %s not ready (dependency missing), retrying", name)
        except Exception as exc:  # noqa: BLE001 - a background task must never die silently
            logger.exception(
                "Task %s crashed (%s), restarting in %.1fs", name, type(exc).__name__, restart_delay
            )
        else:
            logger.warning("Task %s returned unexpectedly, restarting in %.1fs", name, restart_delay)

        try:
            await sleep_or_cancel(restart_delay, name)
        except TaskCancelled:
            return


# ======================================================================= heartbeat sweeper
async def _operator_out(session: AsyncSession, operator: Any) -> dict[str, Any]:
    """Build ``OperatorOut`` for ``SHIFT_CHANGED``; falls back to a contract-shaped dict.

    ``session`` is accepted for parity with future variants that need relationship loading.
    """
    try:
        from app import schemas as schemas_module  # lazy: schemas agent owns this module

        schema_cls = getattr(schemas_module, "OperatorOut", None)
        if schema_cls is not None:
            dumped = schema_cls.model_validate(operator, from_attributes=True).model_dump(mode="json")
            if isinstance(dumped, dict) and dumped:
                return dumped
    except Exception as exc:  # noqa: BLE001 - schema may not exist yet; fall back below
        logger.debug("OperatorOut serialisation unavailable: %s: %s", type(exc).__name__, exc)

    return {
        "id": int(getattr(operator, "id")),
        "username": str(getattr(operator, "username", "")),
        "display_name": str(getattr(operator, "display_name", "")),
        "role": _label(getattr(operator, "role", "OPERATOR")),
        "shift_status": _label(getattr(operator, "shift_status", "OFFLINE")),
        "is_online_ws": bool(getattr(operator, "is_online_ws", False)),
        "last_seen_at": _iso(getattr(operator, "last_seen_at", None)),
        "sla_target_seconds": int(getattr(operator, "sla_target_seconds", settings.backend_sla_seconds)),
        "created_at": _iso(getattr(operator, "created_at", None)),
    }


async def mark_operator_stale(
    ctx: BackgroundContext,
    operator_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Apply the DB side effect of a heartbeat timeout for one operator.

    Contract (CONTRACTS.md §4): set ``is_online_ws=False`` and, when the operator was
    ``ONLINE``, fall the shift back to ``PAUSE`` — never ``OFFLINE``.

    Returns ``(presence_payload, shift_payload)``; both are ``None`` when nothing changed
    (unknown operator, or already offline).
    """
    from app import models as models_module  # lazy: models agent owns this module

    operator_cls = getattr(models_module, "Operator")
    shift_status_cls = getattr(models_module, "ShiftStatus")
    online_label = _label(shift_status_cls.ONLINE)
    pause_label = _label(shift_status_cls.PAUSE)

    async with ctx.session_factory() as session:
        operator = await session.get(operator_cls, int(operator_id))
        if operator is None:
            logger.warning("heartbeat sweep: operator %s no longer exists", operator_id)
            return None, None

        current_label = _label(getattr(operator, "shift_status", pause_label))
        was_online = bool(getattr(operator, "is_online_ws", False))
        if not was_online and current_label != online_label:
            return None, None

        operator.is_online_ws = False
        operator.last_seen_at = utcnow()
        shift_changed = current_label == online_label
        if shift_changed:
            shift_status_cls = getattr(models_module, "ShiftStatus")
            operator.shift_status = shift_status_cls.PAUSE

        await session.commit()

        presence: dict[str, Any] = {"operator_id": int(operator_id), "is_online_ws": False}
        shift_payload: dict[str, Any] | None = None
        if shift_changed:
            presence["shift_status"] = pause_label
            shift_payload = await _operator_out(session, operator)
        return presence, shift_payload


async def heartbeat_sweeper(interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Detect websocket clients that stopped sending HEARTBEAT and mark them offline."""
    period = max(0.5, float(interval))

    async def _body() -> None:
        from app.database import get_session_factory  # lazy

        timeout = float(settings.backend_ws_heartbeat_timeout_seconds)
        timed_out = await manager.sweep_stale(timeout)
        if timed_out:
            ctx = BackgroundContext(session_factory=get_session_factory(), broadcast=manager.broadcast)
            for operator_id in timed_out:
                # Never resurrect presence for an operator who already reconnected.
                if manager.is_online(operator_id):
                    logger.info("Operator %s reconnected during sweep; skipping offline mark", operator_id)
                    continue
                try:
                    presence, shift_payload = await mark_operator_stale(ctx, operator_id)
                except Exception as exc:  # noqa: BLE001 - one bad row must not abort the sweep
                    logger.warning(
                        "mark_operator_stale(%s) failed: %s: %s", operator_id, type(exc).__name__, exc
                    )
                    continue
                if presence is None:
                    continue
                await ctx.broadcast(envelope(EVT_OPERATOR_PRESENCE, presence))
                if shift_payload is not None:
                    await ctx.broadcast(envelope(EVT_SHIFT_CHANGED, shift_payload))

            try:
                await publish(CH_WS_FANOUT, envelope(EVT_OPERATOR_PRESENCE, {"timed_out": timed_out}))
            except Exception as exc:  # noqa: BLE001 - Redis fan-out is optional
                logger.debug("ws_fanout publish skipped: %s", exc)

        await sleep_or_cancel(period, TASK_HEARTBEAT)

    await _supervised_loop(TASK_HEARTBEAT, _body, period)


# ================================================================================= SLA ticker
_SLA_TICK_ARITY: int | None = None


def _tick_arity(func: Callable[..., Any]) -> int:
    """Decide whether to call ``tick(ctx)`` (arity 1) or ``tick(session_factory, broadcast)`` (2)."""
    global _SLA_TICK_ARITY
    if _SLA_TICK_ARITY is not None:
        return _SLA_TICK_ARITY

    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        _SLA_TICK_ARITY = 2
        return _SLA_TICK_ARITY

    params = list(signature.parameters.values())
    if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params):
        _SLA_TICK_ARITY = 2
        return _SLA_TICK_ARITY

    required_positional = [
        param
        for param in params
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and param.default is inspect.Parameter.empty
    ]
    total_positional = [
        param
        for param in params
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]

    # Bound methods / functions declared as ``tick(ctx)`` -> 1; anything wider -> the
    # documented bracket form ``tick(session_factory, broadcast)``.
    _SLA_TICK_ARITY = 1 if len(required_positional) <= 1 and len(total_positional) <= 1 else 2
    logger.info("sla_engine.tick detected with arity %d", _SLA_TICK_ARITY)
    return _SLA_TICK_ARITY


async def _call_sla_tick(tick: Callable[..., Awaitable[None]], ctx: BackgroundContext) -> None:
    """Invoke ``sla_engine.tick`` in the form the service agent implemented."""
    if _tick_arity(tick) == 1:
        await tick(ctx)
    else:
        await tick(ctx.session_factory, ctx.broadcast)


def _assignee_name(chat: Any) -> str | None:
    """Resolve the assignee display name without ever triggering a lazy async load.

    ``GroupChat.assigned_operator`` is ``lazy="selectin"`` so it is already populated by the
    query; the attribute is still read defensively because a lazy ``select`` relationship
    would raise ``MissingGreenlet`` inside an async context.
    """
    assignee = getattr(chat, "assigned_operator", None)
    if assignee is not None:
        return str(getattr(assignee, "display_name", "") or "") or None
    operator_id = getattr(chat, "assigned_operator_id", None)
    if operator_id is not None:
        return f"Operator #{operator_id}"
    return None


async def sla_snapshot(ctx: BackgroundContext) -> list[dict[str, Any]]:
    """Open incidents joined with their chats — the source of the per-tick SLA_UPDATE frames."""
    from app import models as models_module  # lazy

    incident_cls = getattr(models_module, "SLAIncident")
    chat_cls = getattr(models_module, "GroupChat")
    status_cls = _enum_attr(models_module, "SlaIncidentStatus", "SLAIncidentStatus")

    pending_label = _label(status_cls.PENDING)
    stmt = (
        select(incident_cls, chat_cls)
        .join(chat_cls, incident_cls.chat_id == chat_cls.id)
        .where(incident_cls.status == pending_label)
        .order_by(incident_cls.deadline_at.asc())
    )

    now = utcnow()
    frames: list[dict[str, Any]] = []
    async with ctx.session_factory() as session:
        for incident, chat in (await session.execute(stmt)).all():
            deadline = getattr(incident, "deadline_at", None)
            remaining = 0
            if isinstance(deadline, datetime):
                deadline_aware = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)
                remaining = int((deadline_aware - now).total_seconds())
            frames.append(
                {
                    "chat_id": int(getattr(chat, "id")),
                    "incident_id": int(getattr(incident, "id")),
                    "status": _label(getattr(incident, "status", pending_label)),
                    "deadline_at": _iso(deadline),
                    "remaining_seconds": remaining,
                    "assigned_operator_id": getattr(chat, "assigned_operator_id", None),
                    "assigned_operator_name": _assignee_name(chat),
                    "is_overdue": remaining < 0,
                }
            )
    return frames


async def sla_ticker(interval: float | None = None) -> None:
    """Tick the SLA engine, then broadcast one ``SLA_UPDATE`` per open incident."""
    period = float(settings.backend_ws_tick_seconds if interval is None else interval)
    period = max(0.25, period)

    async def _body() -> None:
        from app.database import get_session_factory  # lazy

        try:
            from app.services import sla_engine  # lazy: services agent owns this module
        except ImportError as exc:
            logger.warning("sla_ticker: app.services.sla_engine unavailable (%s)", exc)
            raise _RetrySignal from exc

        ctx = BackgroundContext(session_factory=get_session_factory(), broadcast=manager.broadcast)
        await _call_sla_tick(sla_engine.tick, ctx)

        for frame in await sla_snapshot(ctx):
            await ctx.broadcast(envelope(EVT_SLA_UPDATE, frame))

        await sleep_or_cancel(period, TASK_SLA)

    await _supervised_loop(TASK_SLA, _body, max(TICKER_RETRY_SECONDS, period))


# =========================================================================== Redis consumer
async def _dispatch(channel: str, payload: dict[str, Any], ctx: BackgroundContext) -> None:
    """Resolve and call the ``tg_ingest`` handler for ``channel``.

    Documented signatures (services agent):
        async def handle_incoming(session_factory, broadcast, payload) -> None
        async def handle_deleted(session_factory, broadcast, payload) -> None
        async def handle_edit(session_factory, broadcast, payload) -> None
        async def handle_outgoing_ack(session_factory, broadcast, payload) -> None
    """
    handler_name = CHANNEL_HANDLERS.get(channel)
    if handler_name is None:
        logger.warning("redis_consumer: unknown channel %r", channel)
        return

    try:
        from app.services import tg_ingest  # lazy: services agent owns this module
    except ImportError as exc:
        logger.error("redis_consumer: app.services.tg_ingest unavailable (%s); dropping %s", exc, channel)
        return

    handler = getattr(tg_ingest, handler_name, None)
    if handler is None:
        logger.error("redis_consumer: tg_ingest.%s missing; dropping %s", handler_name, channel)
        return

    await handler(ctx.session_factory, ctx.broadcast, payload)


async def redis_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    broadcast: BroadcastFn,
) -> None:
    """Consume worker -> backend channels forever, reconnecting with 1s..30s backoff."""
    ctx = BackgroundContext(session_factory=session_factory, broadcast=broadcast)

    async def _body() -> None:
        backoff = BACKOFF_INITIAL_SECONDS
        while True:
            ps = await pubsub()
            if ps is None:
                logger.warning("redis_consumer: Redis unavailable, retrying in %.1fs", backoff)
            else:
                try:
                    await ps.subscribe(*CHANNEL_HANDLERS.keys())
                    backoff = BACKOFF_INITIAL_SECONDS
                    async for channel, data in ps.listen():
                        try:
                            await _dispatch(channel, data, ctx)
                        except Exception as exc:  # noqa: BLE001 - one bad payload must not kill the bus
                            logger.exception(
                                "redis_consumer: handler for %s failed (%s)", channel, type(exc).__name__
                            )
                    logger.warning("redis_consumer: subscription ended, reconnecting")
                except TaskCancelled:
                    await ps.close()
                    raise
                except asyncio.CancelledError:
                    await ps.close()
                    raise
                except Exception as exc:  # noqa: BLE001 - transport/parse errors: back off and retry
                    logger.warning(
                        "redis_consumer: connection error (%s: %s), retrying in %.1fs",
                        type(exc).__name__,
                        exc,
                        backoff,
                    )
                finally:
                    await ps.close()

            await sleep_or_cancel(backoff, TASK_REDIS)
            wait_slept().set()
            backoff = min(BACKOFF_MAX_SECONDS, backoff * 2)

    await _supervised_loop(TASK_REDIS, _body, BACKOFF_INITIAL_SECONDS)


# ========================================================================= task supervision
def _register_task(app: Any, name: str, coro: Awaitable[None]) -> asyncio.Task[None]:
    task: asyncio.Task[None] = asyncio.create_task(coro, name=f"tgdesk-{name}")  # type: ignore[arg-type]
    tasks: dict[str, asyncio.Task[None]] = dict(getattr(app.state, "tasks", None) or {})
    tasks[name] = task
    app.state.tasks = tasks
    logger.info("Background task started: %s", name)
    return task


async def start_all(app: Any) -> list[asyncio.Task[None]]:
    """Start heartbeat, SLA and Redis workers; store them in ``app.state.tasks``."""
    from app.database import get_session_factory  # lazy

    existing: dict[str, asyncio.Task[None]] = dict(getattr(app.state, "tasks", None) or {})
    alive = {name: task for name, task in existing.items() if not task.done()}
    if alive:
        logger.warning("start_all: tasks already running (%s); not starting twice", ", ".join(alive))
        app.state.tasks = alive
        return list(alive.values())

    factory = get_session_factory()
    return [
        _register_task(app, TASK_HEARTBEAT, heartbeat_sweeper(HEARTBEAT_INTERVAL_SECONDS)),
        _register_task(app, TASK_SLA, sla_ticker(settings.backend_ws_tick_seconds)),
        _register_task(app, TASK_REDIS, redis_consumer(factory, manager.broadcast)),
    ]


async def stop_all(app: Any, timeout: float = 10.0) -> None:
    """Cancel every background task and wait for it to unwind. Never raises."""
    tasks: dict[str, asyncio.Task[None]] = dict(getattr(app.state, "tasks", None) or {})
    if not tasks:
        logger.info("stop_all: no background tasks to stop")
        return

    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            logger.info("Background task cancelled: %s", name)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("stop_all: %d task(s) did not stop within %.0fs", len(tasks), timeout)
        results = []

    for name, result in zip(tasks.keys(), results, strict=False):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.warning("Background task %s ended with %s", name, type(result).__name__)

    app.state.tasks = {}
    logger.info("stop_all: %d background task(s) stopped", len(tasks))


def task_status(app: Any) -> dict[str, bool]:
    """``{"heartbeat_sweeper": True, ...}`` — live-task flags for ``/api/system/info``."""
    tasks: dict[str, asyncio.Task[None]] = dict(getattr(app.state, "tasks", None) or {})
    return {name: (not task.done()) for name, task in tasks.items()}


__all__ = [
    "BACKOFF_INITIAL_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "CHANNEL_HANDLERS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "TASK_HEARTBEAT",
    "TASK_NAMES",
    "TASK_REDIS",
    "TASK_SLA",
    "BackgroundContext",
    "BroadcastFn",
    "TaskCancelled",
    "heartbeat_sweeper",
    "mark_operator_stale",
    "redis_consumer",
    "sla_snapshot",
    "sla_ticker",
    "sleep_or_cancel",
    "start_all",
    "stop_all",
    "task_status",
    "utcnow",
    "wait_slept",
]

"""Emoji reactions on chat messages (CONTRACTS_V2.md §2.2 / §3.2 / §4).

Business rules
--------------
* The CRM offers exactly six emoji (:class:`~app.enums.ReactionEmoji`); anything
  else is rejected with :class:`ReactionValidationError`.
* **One operator — one reaction per message.** The database cannot enforce this:
  ``message_reactions`` carries two *partial* unique indexes
  (``uq_message_reactions_message_operator_emoji`` /
  ``uq_message_reactions_message_tg_user_emoji``) and ``emoji`` is part of both
  keys, so a second row with a *different* emoji is perfectly legal at the SQL
  level. Every write path here therefore deletes **all** previous rows of the
  acting operator for the message in the *same transaction* as the insert. The
  delete/insert pair additionally runs while holding ``SELECT ... FOR UPDATE``
  on the message row, which serialises two concurrent ``PUT``s of one operator
  (two tabs, an impatient double-click, a retrying client): the last committed
  writer always leaves exactly one row behind.
* Telegram reactions (``source=TELEGRAM``, identified by ``tg_user_id``) are
  mirrored one-way from the worker's ``tg_reactions`` channel. Because no
  ``operator_id`` is involved, two different Telegram users reacting with the
  same emoji produce two rows; :func:`summarize_reactions` counts *distinct*
  ``tg_user_id`` values so the counter always matches Telegram itself.
* Reads are **batched**: :func:`summarize_reactions` costs at most two queries
  for a whole page of messages (aggregation + operator display names), never an
  ``N+1`` loop.

Events (CONTRACTS_V2.md §4)
---------------------------
After a successful commit the service publishes

* ``MESSAGE_REACTION`` — ``{message_id, chat_id, emoji, count, operator_id}``;
* ``MESSAGE_REACTION_REMOVED`` — ``{message_id, chat_id, emoji, operator_id}``.

Both go through :func:`app.ws_manager.manager.broadcast` (plus, best effort, the
``ws_fanout`` Redis channel) exactly like :mod:`app.services.typing` does. The
event-name constants live **here** so ``ws_manager.py`` — owned by another agent
— stays untouched.

Errors are domain-level (:class:`MessageReactionNotFoundError`,
:class:`ReactionValidationError`); the router translates them into ``404`` /
``422``. Database-agnostic: every statement and expression used below works on
both PostgreSQL (production) and SQLite (automated tests).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ReactionEmoji
from app.models import (
    REACTION_SOURCE_CRM,
    REACTION_SOURCE_TELEGRAM,
    GroupChat,
    GroupMessage,
    MessageReaction,
    Operator,
)
from app.schemas import ReactionListOut, ReactionSummaryOut
from app.utils import utcnow
from app.ws_manager import envelope, manager

logger = logging.getLogger("tgdesk.reactions")

__all__ = [
    "EVT_MESSAGE_REACTION",
    "EVT_MESSAGE_REACTION_REMOVED",
    "ClearReactionResult",
    "MessageReactionNotFoundError",
    "REACTION_EVENT_CHANNEL",
    "ReactionError",
    "ReactionValidationError",
    "apply_telegram_reactions",
    "build_mirror_plan",
    "clear_reaction",
    "deliver_mirror_plan",
    "drain_background_tasks",
    "message_exists",
    "normalise_emoji",
    "ordered_summary",
    "pending_delivery_tasks",
    "schedule_reaction_delivery",
    "publish_reaction_to_telegram",
    "set_reaction",
    "summarize_reactions",
    "telegram_reaction_rows",
]

#: ``MESSAGE_REACTION`` — a reaction was placed or replaced (CONTRACTS_V2.md §4).
EVT_MESSAGE_REACTION: Final[str] = "MESSAGE_REACTION"

#: ``MESSAGE_REACTION_REMOVED`` — an operator took their reaction back.
EVT_MESSAGE_REACTION_REMOVED: Final[str] = "MESSAGE_REACTION_REMOVED"

#: Redis channel the backend fans WebSocket frames out to other processes on.
REACTION_EVENT_CHANNEL: Final[str] = "ws_fanout"

#: How many times a write is attempted when a concurrent writer wins the race.
_MAX_WRITE_ATTEMPTS: Final[int] = 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReactionError(Exception):
    """Base class for every domain error raised by this service."""


class MessageReactionNotFoundError(ReactionError):
    """The target message does not exist (the router answers ``404``)."""

    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        super().__init__(f"Сообщение {self.message_id} не найдено")


class ReactionValidationError(ReactionError):
    """The supplied emoji is not one of the six supported reactions (``422``)."""

    def __init__(self, emoji: Any) -> None:
        self.emoji = emoji
        allowed = ", ".join(ReactionEmoji.values())
        super().__init__(f"Недопустимая реакция {emoji!r}. Допустимые: {allowed}")


@dataclass(frozen=True, slots=True)
class ClearReactionResult:
    """Outcome of :func:`clear_reaction`.

    ``removed`` reports whether anything was deleted (``False`` keeps the route
    idempotent); ``reactions`` is the aggregate read inside the same transaction,
    so the router never has to re-query and can never answer with a stale list.
    """

    removed: bool
    reactions: ReactionListOut


@dataclass(frozen=True, slots=True)
class _WriteOutcome:
    """What :func:`_write_operator_reaction` hands back to :func:`set_reaction`.

    Carries the message coordinates needed by the Telegram mirror, the aggregate
    read before ``COMMIT`` and whether the stored emoji actually changed (a no-op
    ``PUT`` must not be mirrored back to Telegram).
    """

    chat_id: int
    tg_msg_id: int | None
    tg_chat_id: int | None
    items: list[ReactionSummaryOut]
    changed: bool


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalise_emoji(emoji: Any) -> str:
    """Return the canonical ``ReactionEmoji`` value or raise :class:`ReactionValidationError`.

    Accepts an enum member, the bare glyph (``"👍"``), an enum member name
    (``"THUMBS_UP"``) and their lower-case spellings, so a legacy client that
    sends names instead of glyphs keeps working.
    """
    if isinstance(emoji, ReactionEmoji):
        return emoji.value
    raw = getattr(emoji, "value", emoji)
    candidate = "" if raw is None else str(raw).strip()
    if not candidate:
        raise ReactionValidationError(emoji)
    try:
        return ReactionEmoji(candidate).value
    except ValueError:
        pass
    try:
        return ReactionEmoji(candidate.upper()).value
    except ValueError:
        pass
    member = getattr(ReactionEmoji, candidate.upper(), None)
    if isinstance(member, ReactionEmoji):
        return member.value
    raise ReactionValidationError(emoji)


def _operator_id(operator: Any) -> int:
    """``operator.id`` as a positive ``int`` (raises when the object carries none)."""
    try:
        value = int(getattr(operator, "id"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReactionValidationError("оператор без идентификатора") from exc
    if value <= 0:
        raise ReactionValidationError("оператор без идентификатора")
    return value


def _to_int(value: Any) -> int | None:
    """Best-effort integer coercion for ids coming from Telegram JSON."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_key(message_id: Any) -> int:
    """Validate a message id, raising :class:`MessageReactionNotFoundError` when unusable."""
    value = _to_int(message_id)
    if value is None or value <= 0:
        raise MessageReactionNotFoundError(value if value is not None else 0)
    return value


#: Pre-computed ``emoji -> position`` table (was rebuilt on every sort key call).
_EMOJI_ORDER: Final[dict[str, int]] = {
    member.value: index for index, member in enumerate(ReactionEmoji)
}


def _order_key(emoji: str) -> tuple[int, str]:
    """Sort aggregated emoji by the contract order of :class:`ReactionEmoji`.

    Unknown emoji (a legacy or Telegram-only glyph) sort last but stay visible:
    hiding a reaction the user can see in Telegram would be worse than showing a
    glyph the CRM picker does not offer.
    """
    return (_EMOJI_ORDER.get(emoji, len(_EMOJI_ORDER)), emoji)


def ordered_summary(items: Sequence[ReactionSummaryOut]) -> list[ReactionSummaryOut]:
    """Return ``items`` in contract order (pure helper, shared with :func:`summarize_reactions`)."""
    return sorted(items, key=lambda item: _order_key(item.emoji))


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


async def message_exists(db: AsyncSession, message_id: int) -> bool:
    """``True`` when a ``group_messages`` row with this id exists."""
    value = _to_int(message_id)
    if value is None or value <= 0:
        return False
    found = (
        await db.execute(select(GroupMessage.id).where(GroupMessage.id == value).limit(1))
    ).scalar_one_or_none()
    return found is not None


async def _operator_emoji_on(
    db: AsyncSession,
    *,
    message_id: int,
    operator_id: int,
) -> list[str]:
    """Emoji values ``operator_id`` currently holds on ``message_id`` (insert order)."""
    rows = (
        await db.execute(
            select(MessageReaction.emoji)
            .where(
                MessageReaction.message_id == int(message_id),
                MessageReaction.operator_id == int(operator_id),
            )
            .order_by(MessageReaction.id.asc())
        )
    ).scalars().all()
    seen: list[str] = []
    for value in rows:
        text = str(value or "")
        if text and text not in seen:
            seen.append(text)
    return seen


# ---------------------------------------------------------------------------
# Aggregation (batched — never N+1)
# ---------------------------------------------------------------------------


async def summarize_reactions(
    db: AsyncSession,
    *,
    message_ids: list[int],
    viewer_operator_id: int | None,
) -> dict[int, list[ReactionSummaryOut]]:
    """Aggregate reactions for ``message_ids`` in at most two queries.

    Query 1 groups by ``(message_id, emoji)`` and counts **distinct**
    ``operator_id`` and **distinct** ``tg_user_id`` values, so one person can
    never be counted twice no matter how many rows the mirror produced.
    Query 2 joins ``operators`` to resolve every reactor id and display name of
    the batch at once.

    Returns ``{message_id: [ReactionSummaryOut, ...]}`` containing an entry for
    *every* requested message (an empty list when it has no reactions) so a
    caller can index the mapping without ``KeyError``. ``me=True`` marks the
    emoji the viewing operator placed.
    """
    clean_ids = sorted(
        {value for value in (_to_int(item) for item in (message_ids or ())) if value is not None and value > 0}
    )
    result: dict[int, list[ReactionSummaryOut]] = {message_id: [] for message_id in clean_ids}
    if not clean_ids:
        return result

    operator_count = func.count(func.distinct(MessageReaction.operator_id))
    tg_count = func.count(func.distinct(MessageReaction.tg_user_id))
    #: A frame that carried only ``count`` is mirrored as **one** row with
    #: ``tg_user_id IS NULL`` standing for an unknown number of people. SQL's
    #: ``COUNT(DISTINCT tg_user_id)`` ignores ``NULL`` entirely, so those rows would
    #: render a counter of 0 — the aggregate is added back as a boolean below, in the
    #: very same query, instead of a second round trip.
    anonymous_count = func.count(
        case(
            (
                (MessageReaction.source == REACTION_SOURCE_TELEGRAM)
                & MessageReaction.tg_user_id.is_(None),
                MessageReaction.id,
            ),
            else_=None,
        )
    )
    rows = (
        await db.execute(
            select(
                MessageReaction.message_id,
                MessageReaction.emoji,
                operator_count.label("operator_count"),
                tg_count.label("tg_count"),
                anonymous_count.label("anonymous_count"),
            )
            .where(MessageReaction.message_id.in_(clean_ids))
            .group_by(MessageReaction.message_id, MessageReaction.emoji)
        )
    ).all()

    counts: dict[int, list[tuple[str, int]]] = {}
    for message_id, emoji, operators_count, telegram_count, anonymous in rows:
        text = str(emoji or "")
        if not text:
            continue
        total = int(operators_count or 0) + int(telegram_count or 0)
        if int(anonymous or 0):
            total += 1
        counts.setdefault(int(message_id), []).append((text, max(total, 0)))
    if not counts:
        return result

    # Query 2: reactor identity + display names for the whole batch in a single
    # LEFT JOIN, so one page costs exactly two round trips regardless of size.
    reactor_rows = (
        await db.execute(
            select(
                MessageReaction.message_id,
                MessageReaction.emoji,
                MessageReaction.operator_id,
                Operator.display_name,
                Operator.username,
            )
            .outerjoin(Operator, Operator.id == MessageReaction.operator_id)
            .where(
                MessageReaction.message_id.in_(clean_ids),
                MessageReaction.operator_id.is_not(None),
            )
            .order_by(MessageReaction.id.asc())
        )
    ).all()

    reactors: dict[tuple[int, str], list[int]] = {}
    names: dict[int, str] = {}
    for message_id, emoji, operator_id, display_name, username in reactor_rows:
        value = _to_int(operator_id)
        if value is None:
            continue
        bucket = reactors.setdefault((int(message_id), str(emoji or "")), [])
        if value not in bucket:
            bucket.append(value)
        if value not in names:
            label = str(display_name or "").strip() or str(username or "").strip()
            names[value] = label or f"Оператор #{value}"

    viewer = _to_int(viewer_operator_id)
    for message_id, buckets in counts.items():
        summaries: list[ReactionSummaryOut] = []
        for emoji, count in sorted(buckets, key=lambda item: _order_key(item[0])):
            ids = reactors.get((message_id, emoji), [])
            summaries.append(
                ReactionSummaryOut(
                    emoji=emoji,
                    count=count,
                    me=bool(viewer is not None and viewer in ids),
                    operators=[names.get(value, f"Оператор #{value}") for value in ids],
                    operator_ids=list(ids),
                )
            )
        result[message_id] = summaries
    return result


async def _summary_for(
    db: AsyncSession,
    *,
    message_id: int,
    viewer_operator_id: int | None,
) -> ReactionListOut:
    """The aggregated :class:`ReactionListOut` for exactly one message."""
    mapping = await summarize_reactions(
        db, message_ids=[int(message_id)], viewer_operator_id=viewer_operator_id
    )
    return ReactionListOut(items=mapping.get(int(message_id), []))


async def _count_for(db: AsyncSession, *, message_id: int, emoji: str) -> int:
    """Counter of ``emoji`` on ``message_id`` (distinct operators + distinct TG users)."""
    row = (
        await db.execute(
            select(
                func.count(func.distinct(MessageReaction.operator_id)),
                func.count(func.distinct(MessageReaction.tg_user_id)),
            ).where(
                MessageReaction.message_id == int(message_id),
                MessageReaction.emoji == emoji,
            )
        )
    ).one()
    return max(int(row[0] or 0) + int(row[1] or 0), 0)


# ---------------------------------------------------------------------------
# Event fan-out
# ---------------------------------------------------------------------------


async def _broadcast(event_type: str, data: Mapping[str, Any]) -> None:
    """Fan one envelope out over WebSocket, then (best effort) over Redis.

    Both transports are optional: the write is already committed, so a delivery
    problem is logged and swallowed instead of failing the HTTP request.
    """
    payload = dict(data)
    try:
        await manager.broadcast(envelope(event_type, payload))
    except Exception as exc:  # noqa: BLE001 - a socket hiccup must not undo the write
        logger.warning("broadcast of %s failed: %s: %s", event_type, type(exc).__name__, exc)
    try:
        from app.redis_bus import publish

        await publish(REACTION_EVENT_CHANNEL, envelope(event_type, payload))
    except Exception as exc:  # noqa: BLE001 - multi-process fan-out is optional
        logger.debug("publish of %s skipped: %s: %s", event_type, type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Mirror target and delivery, on primitives only
# ---------------------------------------------------------------------------


#: A ready-to-publish Telegram mirror command:
#: ``(event_type, ws_payload, redis_channel, redis_payload)``.
#:
#: ``redis_payload`` is ``None`` when there is nothing to mirror (the message never
#: reached Telegram). Both dictionaries carry **plain values only**, which is what
#: lets the delivery run after the request session has been closed.
MirrorPlan = tuple[str, dict[str, Any], str | None, dict[str, Any] | None]


def build_mirror_plan(
    *,
    event_type: str,
    ws_payload: Mapping[str, Any],
    message_id: int,
    chat_id: int | None,
    tg_msg_id: int | None,
    tg_chat_id: int | None,
    emoji: str | None,
    operator_username: str | None,
    cmd_id: str | None = None,
) -> MirrorPlan:
    """Freeze the WS frame and the Telegram mirror command into primitives.

    Called **inside** the request coroutine while the message row and its chat are
    still at hand (:func:`_write_operator_reaction` already selected both), so the
    post-commit work needs neither the ORM session nor a second ``SELECT``.
    """
    ws = dict(ws_payload)
    if tg_msg_id is None or tg_chat_id is None or chat_id is None:
        logger.debug(
            "message %s has no Telegram counterpart yet, reaction stays CRM-only", message_id
        )
        return (event_type, ws, None, None)

    from app.redis_bus import CH_TG_REACTION_OUT  # noqa: PLC0415 - keep the module import-light

    command = {
        "cmd_id": cmd_id or str(uuid.uuid4()),
        "action": "SET" if emoji else "CLEAR",
        "message_id": int(message_id),
        "chat_id": int(chat_id),
        "tg_chat_id": int(tg_chat_id),
        "tg_msg_id": int(tg_msg_id),
        "emoji": emoji,
        "operator_username": operator_username,
    }
    return (event_type, ws, CH_TG_REACTION_OUT, command)


async def deliver_mirror_plan(plan: MirrorPlan, *, message_id: int) -> None:
    """Publish a frozen :data:`MirrorPlan`: WS frame first, then the Telegram command.

    The WS frame goes out **before** the worker command on purpose: the message
    itself took ~70 ms while the frame lands 1–2 ms after it, so a client that
    receives the frame first never renders a counter that lags its own state.
    Every failure is logged and swallowed — the CRM write is already committed.
    """
    event_type, ws_payload, channel, command = plan
    await _broadcast(event_type, ws_payload)
    if channel is None or command is None:
        return
    try:
        from app.redis_bus import publish

        await publish(channel, command)
        logger.info(
            "reaction %s on message %s queued for Telegram (%s)",
            command.get("emoji") or "CLEAR",
            message_id,
            command.get("cmd_id"),
        )
    except Exception as exc:  # noqa: BLE001 - Telegram is a mirror, never the source of truth
        logger.warning(
            "could not queue the reaction mirror for message %s: %s: %s",
            message_id,
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# Background delivery registry
# ---------------------------------------------------------------------------


class _BackgroundTasks:
    """Strong-reference registry for fire-and-forget delivery coroutines.

    ``asyncio`` only keeps a **weak** reference to a task, so a task that is not
    stored anywhere may be garbage-collected before it runs and its exception is
    then swallowed by the event loop. Every task started here is therefore:

    * kept in :attr:`_tasks` until it finishes (the done-callback removes it);
    * inspected by :meth:`_on_done`, which logs ``CancelledError`` at debug and any
      other outcome with :meth:`logging.Logger.error` (the coroutine bodies already
      swallow transport errors, so a non-cancelled exception here is a bug);
    * optionally drained by :meth:`drain` at shutdown so a pending frame is not
      lost when the process is asked to stop.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        """Number of delivery tasks that have not finished yet (introspection/tests)."""
        return len(self._tasks)

    def spawn(self, coro: Awaitable[None], *, label: str) -> asyncio.Task[None] | None:
        """Schedule ``coro``; returns the task, or ``None`` when there is no loop.

        A missing running loop only happens when the service is driven outside of
        an ASGI request (a script, a sync test): the coroutine is closed to avoid a
        "never awaited" warning and the caller keeps its committed result.
        """
        try:
            task = asyncio.get_running_loop().create_task(coro, name=f"{self._name}:{label}")
        except RuntimeError:
            logger.warning("no running loop for background task %s; delivery skipped", label)
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def track(self, task: asyncio.Task[None]) -> None:
        """Adopt an already-created task so it is reference-counted and logged too."""
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            logger.debug("background task %s cancelled", task.get_name())
            return
        exc = task.exception()
        if exc is not None:  # noqa: SIM108 - explicit branch documents the contract
            logger.error(
                "background task %s failed: %s: %s", task.get_name(), type(exc).__name__, exc
            )

    async def drain(self, timeout: float = 2.0) -> None:
        """Wait (bounded) for every pending delivery; never raises."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)
        except (TimeoutError, asyncio.CancelledError):
            logger.warning("%d reaction delivery task(s) did not finish in %.1fs", len(pending), timeout)


#: Process-wide registry of post-commit reaction deliveries.
_delivery = _BackgroundTasks("reaction-delivery")


def pending_delivery_tasks() -> int:
    """How many post-commit reaction deliveries are still in flight."""
    return _delivery.pending


async def drain_background_tasks(timeout: float = 2.0) -> None:
    """Flush pending post-commit deliveries (called on shutdown / by tests)."""
    await _delivery.drain(timeout)


def publish_reaction_to_telegram(
    *,
    message_id: int,
    chat_id: int | None,
    tg_msg_id: int | None,
    tg_chat_id: int | None,
    emoji: str | None,
    operator_username: str | None,
) -> str | None:
    """Mirror a CRM reaction onto the Telegram message (CONTRACTS_V4 §2) — primitives in.

    ``emoji=None`` means ``CLEAR`` (the operator took their reaction back). Returns
    the ``cmd_id`` that was handed to the bus, or ``None`` when there was nothing to
    send — the message has no ``tg_msg_id`` (it never reached Telegram) or its chat
    is not linked to one. Both are normal states, not failures.

    The signature takes **values, not a session**, on purpose: everything needed is
    read while the message row is already locked by the caller
    (:func:`_write_operator_reaction`), so the mirror costs no extra ``SELECT`` and
    cannot outlive the request's ORM session. The publish itself is dispatched as a
    background delivery (see :func:`_schedule_delivery`); CONTRACTS_V4 §4.3 p.3
    requires it to stay outside the HTTP response path.
    """
    if tg_msg_id is None or tg_chat_id is None:
        logger.debug(
            "message %s has no Telegram counterpart yet, reaction stays CRM-only", message_id
        )
        return None

    from app.redis_bus import CH_TG_REACTION_OUT, publish  # noqa: PLC0415

    command = {
        "cmd_id": str(uuid.uuid4()),
        "action": "SET" if emoji else "CLEAR",
        "message_id": int(message_id),
        "chat_id": int(chat_id) if chat_id is not None else None,
        "tg_chat_id": int(tg_chat_id),
        "tg_msg_id": int(tg_msg_id),
        "emoji": emoji,
        "operator_username": operator_username,
    }

    async def _deliver() -> None:
        try:
            await publish(CH_TG_REACTION_OUT, command)
            logger.info(
                "reaction %s on message %s queued for Telegram (%s)",
                emoji or "CLEAR",
                message_id,
                command["cmd_id"],
            )
        except Exception as exc:  # noqa: BLE001 - REST already answered successfully
            logger.warning(
                "could not queue the reaction mirror for message %s: %s: %s",
                message_id,
                type(exc).__name__,
                exc,
            )

    _delivery.spawn(_deliver(), label=f"mirror:{message_id}")
    return str(command["cmd_id"])


async def _schedule_delivery(
    *,
    event_type: str,
    data: Mapping[str, Any],
    message_id: int,
    chat_id: int | None,
    tg_msg_id: int | None,
    tg_chat_id: int | None,
    emoji: str | None,
    operator_username: str | None,
) -> None:
    """Hand the post-commit fan-out + mirror to a tracked background task.

    The plan is frozen here from primitives only, so the background coroutine never
    touches the request's ``AsyncSession`` (it is closed as soon as the response is
    sent) and never opens a second one: the WebSocket frame and the Redis publish
    both go through the shared connection pool of the process.
    """
    schedule_reaction_delivery(
        event_type=event_type,
        data=data,
        message_id=message_id,
        chat_id=chat_id,
        tg_msg_id=tg_msg_id,
        tg_chat_id=tg_chat_id,
        emoji=emoji,
        operator_username=operator_username,
    )


def schedule_reaction_delivery(
    *,
    event_type: str,
    data: Mapping[str, Any],
    message_id: int,
    chat_id: int | None,
    tg_msg_id: int | None,
    tg_chat_id: int | None,
    emoji: str | None,
    operator_username: str | None,
) -> None:
    """Freeze the plan from primitives and dispatch it (synchronous call site)."""
    plan = build_mirror_plan(
        event_type=event_type,
        ws_payload=data,
        message_id=message_id,
        chat_id=chat_id,
        tg_msg_id=tg_msg_id,
        tg_chat_id=tg_chat_id,
        emoji=emoji,
        operator_username=operator_username,
    )
    _delivery.spawn(deliver_mirror_plan(plan, message_id=message_id), label=f"{event_type}:{message_id}")


# ---------------------------------------------------------------------------
# Operator writes
# ---------------------------------------------------------------------------


async def _load_message_target(
    db: AsyncSession,
    message_id: int,
    *,
    for_update: bool = False,
) -> tuple[int, int | None, int | None] | None:
    """Return ``(chat_id, tg_msg_id, tg_chat_id)`` for the message — ``None`` when absent.

    The chat is joined **here** so the Telegram mirror never needs the second
    ``SELECT`` :func:`publish_reaction_to_telegram` used to run after the commit.
    ``for_update=True`` adds ``SELECT ... FOR UPDATE`` on the message row only
    (``FOR UPDATE OF group_messages``), which is the row the writers serialise on.
    """
    stmt = (
        select(GroupMessage.chat_id, GroupMessage.tg_msg_id, GroupChat.tg_chat_id)
        .join(GroupChat, GroupChat.id == GroupMessage.chat_id)
        .where(GroupMessage.id == int(message_id))
    )
    if for_update:
        stmt = stmt.with_for_update(of=GroupMessage)
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    chat_id, tg_msg_id, tg_chat_id = row
    return (
        int(chat_id),
        _to_int(tg_msg_id),
        _to_int(tg_chat_id),
    )


async def _write_operator_reaction(
    db: AsyncSession,
    *,
    message_id: int,
    operator_id: int,
    emoji: str,
    viewer_operator_id: int,
) -> _WriteOutcome:
    """Delete every previous reaction of the operator, then insert ``emoji``.

    Returns the chat coordinates **and** the aggregated :class:`ReactionListOut`
    read back inside the same transaction (before ``COMMIT``). Doing the aggregate
    here instead of after the commit saves one round trip *and* removes the
    "committed but not yet visible to a pooled reader" window.

    ``SELECT ... FOR UPDATE`` on the message row makes concurrent writers for the
    same message wait for each other, so the ``DELETE`` observes every row
    committed by the loser of the race and exactly one reaction survives. A no-op
    ``PUT`` (same emoji as before) keeps the existing row and its id.
    """
    target = await _load_message_target(db, message_id, for_update=True)
    if target is None:
        raise MessageReactionNotFoundError(message_id)
    chat_id, tg_msg_id, tg_chat_id = target

    previous = await _operator_emoji_on(db, message_id=message_id, operator_id=operator_id)
    if previous != [emoji]:
        await db.execute(
            delete(MessageReaction).where(
                MessageReaction.message_id == int(message_id),
                MessageReaction.operator_id == int(operator_id),
            )
        )
        db.add(
            MessageReaction(
                message_id=int(message_id),
                operator_id=int(operator_id),
                emoji=emoji,
                tg_user_id=None,
                source=REACTION_SOURCE_CRM,
                created_at=utcnow(),
            )
        )
    # Always flush: an UPDATE/DELETE/INSERT must reach the database inside the
    # ``FOR UPDATE`` window, so a concurrent writer's rollback sees our rows.
    await db.flush()

    items = (
        await _summary_for(db, message_id=int(message_id), viewer_operator_id=viewer_operator_id)
    ).items
    return _WriteOutcome(
        chat_id=chat_id,
        tg_msg_id=tg_msg_id,
        tg_chat_id=tg_chat_id,
        items=items,
        changed=previous != [emoji],
    )


def _schedule_reaction_delivery(
    *,
    event_type: str,
    data: Mapping[str, Any],
    message_id: int,
    chat_id: int | None,
    tg_msg_id: int | None,
    tg_chat_id: int | None,
    emoji: str | None,
    operator_username: str | None,
) -> None:
    """Fire-and-forget variant of :func:`_schedule_delivery` for synchronous callers."""
    schedule_reaction_delivery(
        event_type=event_type,
        data=data,
        message_id=message_id,
        chat_id=chat_id,
        tg_msg_id=tg_msg_id,
        tg_chat_id=tg_chat_id,
        emoji=emoji,
        operator_username=operator_username,
    )


async def set_reaction(
    db: AsyncSession,
    *,
    message_id: int,
    operator: Operator,
    emoji: str,
) -> ReactionListOut:
    """Place (or replace) ``operator``'s reaction on ``message_id``.

    Contract §3.2: a repeated ``PUT`` with a different emoji **replaces** the
    previous one, so the operator never ends up with two reactions.

    * :class:`MessageReactionNotFoundError` — the message does not exist (404);
    * :class:`ReactionValidationError` — the emoji is not one of the six (422);
    * ``IntegrityError`` — re-raised only after a second, rolled-back attempt
      (a concurrent writer won twice in a row).

    Critical path is **one transaction, no lock wait of its own**:

    1. ``SELECT ... FOR UPDATE`` of the message (joins its chat — the mirror
       coordinates are taken from the same row);
    2. the operator's current emoji, the ``DELETE`` + ``INSERT`` and the
       aggregate read, all before ``COMMIT``;
    3. the ``MESSAGE_REACTION`` frame and the Telegram mirror are dispatched as a
       tracked background task **after** the commit, so neither the socket fan-out
       nor Redis can delay the HTTP response (CONTRACTS_V4 §4.3 p.3). The task
       carries plain values only and never touches this session.
    """
    value = normalise_emoji(emoji)
    actor_id = _operator_id(operator)
    message_key = _message_key(message_id)
    username = getattr(operator, "username", None)

    outcome: _WriteOutcome | None = None
    for attempt in range(1, _MAX_WRITE_ATTEMPTS + 1):
        try:
            outcome = await _write_operator_reaction(
                db,
                message_id=message_key,
                operator_id=actor_id,
                emoji=value,
                viewer_operator_id=actor_id,
            )
            await db.commit()
            break
        except IntegrityError as exc:
            # A concurrent writer inserted the same (message, operator, emoji)
            # key between our SELECT and INSERT. Roll back so the session is
            # usable again, then replay the whole delete/insert pair once.
            await db.rollback()
            if attempt >= _MAX_WRITE_ATTEMPTS:
                logger.error(
                    "reaction write for message %s by operator %s failed twice (%s)",
                    message_key,
                    actor_id,
                    type(exc).__name__,
                )
                raise
            logger.info(
                "reaction write race on message %s by operator %s, retrying",
                message_key,
                actor_id,
            )
            # Purpose-built existence probe: one cheap projection instead of
            # loading the ORM entity, and the retry then reloads the message
            # itself under FOR UPDATE anyway.
            if not await message_exists(db, message_key):
                raise MessageReactionNotFoundError(message_key) from exc

    if outcome is None:  # pragma: no cover - the loop either breaks or raises
        raise MessageReactionNotFoundError(message_key)

    count = next((item.count for item in outcome.items if item.emoji == value), 0)
    if outcome.changed:
        # CONTRACTS_V4 §2: mirror onto Telegram after the CRM commit. A no-op PUT
        # (same emoji as before) is not a change, so nothing is mirrored.
        _schedule_reaction_delivery(
            event_type=EVT_MESSAGE_REACTION,
            data={
                "message_id": message_key,
                "chat_id": outcome.chat_id,
                "emoji": value,
                "count": int(count),
                "operator_id": actor_id,
            },
            message_id=message_key,
            chat_id=outcome.chat_id,
            tg_msg_id=outcome.tg_msg_id,
            tg_chat_id=outcome.tg_chat_id,
            emoji=value,
            operator_username=username,
        )
    logger.info(
        "operator %s set %s on message %s (count=%d, changed=%s)",
        actor_id,
        value,
        message_key,
        count,
        outcome.changed,
    )
    return ReactionListOut(items=outcome.items)


async def clear_reaction(
    db: AsyncSession,
    *,
    message_id: int,
    operator: Operator,
) -> ClearReactionResult:
    """Remove **every** reaction ``operator`` placed on ``message_id``.

    Returns :class:`ClearReactionResult` — ``removed`` (whether at least one row
    disappeared) plus the freshly aggregated :class:`ReactionListOut`, which the
    router answers with directly instead of running a second ``SELECT`` (the
    aggregate is read inside this transaction, so it also cannot observe a stale
    snapshot).

    The odd emoji are broadcast as ``MESSAGE_REACTION_REMOVED`` and the Telegram
    mirror is queued **after** the commit, in a tracked background task.
    """
    actor_id = _operator_id(operator)
    message_key = _message_key(message_id)
    username = getattr(operator, "username", None)

    target = await _load_message_target(db, message_key, for_update=True)
    if target is None:
        raise MessageReactionNotFoundError(message_key)
    chat_id, tg_msg_id, tg_chat_id = target

    removed = await _operator_emoji_on(db, message_id=message_key, operator_id=actor_id)
    if removed:
        await db.execute(
            delete(MessageReaction).where(
                MessageReaction.message_id == message_key,
                MessageReaction.operator_id == actor_id,
            )
        )
    # One aggregate for both branches: the transaction sees its own DELETE, so a
    # no-op ``DELETE`` (operator had nothing) and a real removal cost the same.
    items = (
        await _summary_for(db, message_id=message_key, viewer_operator_id=actor_id)
    ).items
    await db.commit()

    if not removed:
        logger.debug(
            "operator %s has no reaction on message %s, nothing to clear", actor_id, message_key
        )
        return ClearReactionResult(removed=False, reactions=ReactionListOut(items=items))

    # One delivery task per removed emoji keeps the per-emoji frame payload; the
    # Telegram CLEAR rides on the first one (it is idempotent and account-scoped).
    for index, emoji in enumerate(removed):
        _schedule_reaction_delivery(
            event_type=EVT_MESSAGE_REACTION_REMOVED,
            data={
                "message_id": message_key,
                "chat_id": chat_id,
                "emoji": emoji,
                "operator_id": actor_id,
            },
            message_id=message_key,
            chat_id=chat_id,
            tg_msg_id=tg_msg_id if index == 0 else None,
            tg_chat_id=tg_chat_id if index == 0 else None,
            # CONTRACTS_V4 §2: one CLEAR for the account, not one per emoji —
            # Telegram holds a single reaction per account and removing it is
            # idempotent.
            emoji=None,
            operator_username=username,
        )
    logger.info(
        "operator %s cleared %s reaction(s) on message %s", actor_id, len(removed), message_key
    )
    return ClearReactionResult(removed=True, reactions=ReactionListOut(items=items))


# ---------------------------------------------------------------------------
# Telegram mirror (worker -> backend, ``tg_reactions`` channel)
# ---------------------------------------------------------------------------


def telegram_reaction_rows(
    reactions: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[str, int | None]]:
    """Convert a ``tg_reactions`` payload into ``[(emoji, tg_user_id), ...]``.

    Telegram sends ``{"emoji": "👍", "count": 2, "user_ids": [123, 456]}``. When
    ``user_ids`` is present each id becomes its own row (that is what the
    partial unique index keys on); when only ``count`` is known the reaction is
    stored once with ``tg_user_id=NULL`` so a "N people reacted" frame is still
    mirrored. Duplicates are dropped and unsupported emoji are skipped.

    ``count`` is authoritative, but ``user_ids`` is only what Telegram reveals in
    ``recent_reactions`` — it can be a partial. When ``count`` is larger than the
    listed users, the surplus is unknown and the payload loses information; keeping
    only the listed users would render a smaller counter than Telegram itself, and
    inventing user ids would corrupt the partial unique index. The identified rows
    are therefore stored and the shortfall is reported, so an operator can see why
    the counter differs.
    """
    collected: list[tuple[str, int | None]] = []
    for entry in reactions or ():
        if not isinstance(entry, Mapping):
            continue
        try:
            emoji = normalise_emoji(entry.get("emoji"))
        except ReactionValidationError:
            logger.warning("tg_reactions: unsupported emoji %r, ignored", entry.get("emoji"))
            continue
        user_ids: list[int] = []
        raw_users = entry.get("user_ids")
        if isinstance(raw_users, (list, tuple, set, frozenset)):
            for candidate in raw_users:
                value = _to_int(candidate)
                if value is not None and value not in user_ids:
                    user_ids.append(value)
        single = _to_int(entry.get("tg_user_id"))
        if single is not None and single not in user_ids:
            user_ids.append(single)
        if user_ids:
            collected.extend((emoji, value) for value in user_ids)
            declared = _to_int(entry.get("count"))
            if declared is not None and declared > len(user_ids):
                logger.info(
                    "tg_reactions: %s reported with count=%s but only %d user id(s) — "
                    "mirroring the identified reactors only",
                    emoji,
                    declared,
                    len(user_ids),
                )
        elif (_to_int(entry.get("count")) or 0) > 0:
            collected.append((emoji, None))
    # De-duplicate the (emoji, user) pairs while keeping the first occurrence.
    unique: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for row in collected:
        if row in seen:
            continue
        seen.add(row)
        unique.append(row)
    return unique


async def apply_telegram_reactions(
    db: AsyncSession,
    *,
    chat_id: int,
    tg_msg_id: int,
    reactions: list[dict],
) -> None:
    """Mirror the authoritative Telegram reaction set onto one message.

    All ``source=TELEGRAM`` rows of the message are replaced by ``reactions``;
    reactions placed by CRM operators (``source=CRM``) are never touched. The
    operation is idempotent, so a redelivered frame after a worker restart
    converges on the same state.

    Unknown chat/message ids are logged and ignored (the worker may report a
    reaction before the message itself reached the CRM). This coroutine never
    raises, so one bad frame cannot kill the Redis consumer.
    """
    try:
        chat_key = _to_int(chat_id)
        tg_key = _to_int(tg_msg_id)
        if chat_key is None or tg_key is None:
            logger.warning(
                "tg_reactions: missing chat_id/tg_msg_id (%r/%r), ignored", chat_id, tg_msg_id
            )
            return

        message = (
            await db.execute(
                select(GroupMessage)
                .where(GroupMessage.chat_id == chat_key, GroupMessage.tg_msg_id == tg_key)
                .order_by(GroupMessage.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if message is None:
            logger.info(
                "tg_reactions: message tg_msg_id=%s is not stored in chat %s yet, ignored",
                tg_key,
                chat_key,
            )
            await db.commit()
            return

        message_key = int(message.id)
        previous_rows = (
            await db.execute(
                select(MessageReaction.emoji, MessageReaction.tg_user_id).where(
                    MessageReaction.message_id == message_key,
                    MessageReaction.source == REACTION_SOURCE_TELEGRAM,
                )
            )
        ).all()
        previous = [(str(row[0] or ""), _to_int(row[1])) for row in previous_rows]
        wanted = telegram_reaction_rows(reactions)

        if set(previous) == set(wanted) and len(previous) == len(wanted):
            # Redelivered frame: nothing changed, keep the rows (and their ids).
            await db.commit()
            logger.debug("tg_reactions: message %s already up to date", message_key)
            return

        await db.execute(
            delete(MessageReaction).where(
                MessageReaction.message_id == message_key,
                MessageReaction.source == REACTION_SOURCE_TELEGRAM,
            )
        )
        now = utcnow()
        for emoji, tg_user_id in wanted:
            db.add(
                MessageReaction(
                    message_id=message_key,
                    operator_id=None,
                    emoji=emoji,
                    tg_user_id=tg_user_id,
                    source=REACTION_SOURCE_TELEGRAM,
                    created_at=now,
                )
            )
        await db.flush()
        await db.commit()

        # Every emoji either set changed (added/removed users); publish its fresh
        # counter (or its removal) so clients never display a stale number.
        touched = sorted({emoji for emoji, _ in previous} | {emoji for emoji, _ in wanted}, key=_order_key)
        wanted_emoji = {emoji for emoji, _ in wanted}
        for emoji in touched:
            if emoji in wanted_emoji:
                count = await _count_for(db, message_id=message_key, emoji=emoji)
                remaining_user = next(
                    (user for value, user in wanted if value == emoji), None
                )
                await _broadcast(
                    EVT_MESSAGE_REACTION,
                    {
                        "message_id": message_key,
                        "chat_id": chat_key,
                        "emoji": emoji,
                        "count": int(count),
                        "tg_user_id": remaining_user,
                    },
                )
            else:
                await _broadcast(
                    EVT_MESSAGE_REACTION_REMOVED,
                    {
                        "message_id": message_key,
                        "chat_id": chat_key,
                        "emoji": emoji,
                        "tg_user_id": None,
                    },
                )
        logger.info(
            "tg_reactions: message %s (chat %s) mirrored %d row(s), replaced %d",
            message_key,
            chat_key,
            len(wanted),
            len(previous),
        )
    except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the Redis consumer
        logger.exception("apply_telegram_reactions failed (%s: %s)", type(exc).__name__, exc)
        try:
            await db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001
            logger.debug("rollback after tg_reactions failure failed: %s", rollback_exc)

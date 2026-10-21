"""Editable quick replies (CONTRACTS_V5 §5.2 / §5.3).

Business rules
--------------
* The four canned answers that used to be hard-coded in
  ``frontend/src/components/MessageInput.tsx`` now live in ``quick_replies``
  (§5.4). Everything the API returns is read from the database — no list is
  duplicated in code.
* ``shortcut`` is the natural key (§5.1, ``UNIQUE``). A duplicate is a client
  error, so create/update pre-check it and translate the losing side of a
  concurrent race (:class:`sqlalchemy.exc.IntegrityError`) into the same
  :class:`QuickReplyConflictError` — the caller always sees one ``409``.
* ``created_by_id`` records who authored a reply; deleting that account does not
  delete the reply (the FK is ``ON DELETE SET NULL``), so a canned answer
  outlives its author and keeps working.
* Retired replies (``is_active=False``) are hidden from the operator list but
  stay in the table, are still reachable through ``?all=true`` and can be
  reactivated with a ``PATCH``. Nothing is ever destroyed by deactivation.

Reads
-----
:func:`list_quick_replies` orders by ``(sort_order, id)`` in SQL and loads the
author relationship eagerly (``lazy="selectin"`` on the model), so rendering a
list of *N* replies costs two queries regardless of *N* — never an ``N+1`` loop.

Events (CONTRACTS_V5 §5.3)
--------------------------
After **every** successful mutation the service broadcasts
``QUICK_REPLIES_UPDATED`` carrying the whole current list
(``{"items": [...]}``), so every connected operator's composer updates without a
reload. The frame goes through :func:`app.ws_manager.manager.broadcast` and, best
effort, the ``ws_fanout`` Redis channel — exactly like
:mod:`app.services.reactions`. The event name is a constant of *this* module so
``ws_manager.py`` (owned by another agent) stays untouched.

Errors are domain-level (:class:`QuickReplyNotFoundError`,
:class:`QuickReplyConflictError`, :class:`QuickReplyValidationError`); the router
translates them into ``404`` / ``409`` / ``422``. Every statement below works on
both PostgreSQL (production) and SQLite (the automated tests' backend).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operator, QuickReply
from app.schemas import (
    QuickReplyIn,
    QuickReplyListOut,
    QuickReplyOut,
    QuickReplyUpdate,
    dump_model,
    normalise_quick_reply_body,
    normalise_quick_reply_category,
    normalise_quick_reply_shortcut,
    normalise_quick_reply_title,
)
from app.utils import utcnow
from app.ws_manager import envelope, manager

logger = logging.getLogger("tgdesk.quick_replies")

__all__ = [
    "EVT_QUICK_REPLIES_UPDATED",
    "QUICK_REPLIES_EVENT_CHANNEL",
    "QuickReplyConflictError",
    "QuickReplyError",
    "QuickReplyNotFoundError",
    "QuickReplyValidationError",
    "create_quick_reply",
    "delete_quick_reply",
    "list_quick_replies",
    "quick_replies_payload",
    "update_quick_reply",
]

#: ``QUICK_REPLIES_UPDATED`` — the editable list changed (CONTRACTS_V5 §5.3).
EVT_QUICK_REPLIES_UPDATED: Final[str] = "QUICK_REPLIES_UPDATED"

#: Redis channel the backend fans WebSocket frames out to other processes on.
QUICK_REPLIES_EVENT_CHANNEL: Final[str] = "ws_fanout"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuickReplyError(Exception):
    """Base class for every domain error raised by this service."""


class QuickReplyNotFoundError(QuickReplyError):
    """The target reply does not exist (the router answers ``404``)."""

    def __init__(self, reply_id: Any) -> None:
        self.reply_id = reply_id
        super().__init__(f"Быстрый ответ {reply_id} не найден")


class QuickReplyConflictError(QuickReplyError):
    """``shortcut`` is already taken by another reply (the router answers ``409``)."""

    def __init__(self, shortcut: str) -> None:
        self.shortcut = str(shortcut)
        super().__init__(f"Быстрый ответ с кодом «{self.shortcut}» уже существует")


class QuickReplyValidationError(QuickReplyError):
    """A field failed a domain rule the schema layer cannot express (``422``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reply_key(reply_id: Any) -> int:
    """``reply_id`` as a positive ``int`` (``ValueError`` when unusable)."""
    if isinstance(reply_id, bool):
        raise ValueError("reply_id must be an integer")
    try:
        value = int(reply_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"reply_id must be an integer, got {reply_id!r}") from exc
    if value <= 0:
        raise ValueError(f"reply_id must be a positive integer, got {value}")
    return value


def _operator_id(operator: Any) -> int | None:
    """``operator.id`` as a positive ``int``, or ``None`` for an anonymous actor.

    A missing author is not an error: the seed and internal callers create rows
    without a session, and §5.1 allows ``created_by_id IS NULL``.
    """
    value = getattr(operator, "id", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _as_changes(payload: QuickReplyIn | QuickReplyUpdate | Mapping[str, Any]) -> dict[str, Any]:
    """Normalise the three accepted payload shapes into a validated ``dict``.

    Accepts the two request models as-is (so both a model instance and a plain
    mapping work), re-validating a mapping through :class:`QuickReplyIn` so a
    direct service call cannot bypass the edge rules. Unknown keys are rejected
    by :class:`QuickReplyIn`'s ``extra="forbid"``.
    """
    if isinstance(payload, QuickReplyUpdate):
        return payload.changes()
    if isinstance(payload, QuickReplyIn):
        return payload.model_dump(exclude_none=True)
    if isinstance(payload, Mapping):
        return QuickReplyIn.model_validate(dict(payload)).model_dump(exclude_none=True)
    raise QuickReplyValidationError(
        f"payload must be QuickReplyIn, QuickReplyUpdate or a mapping, got {type(payload).__name__}"
    )


async def _load_reply(db: AsyncSession, reply_id: int) -> QuickReply | None:
    """The reply row, or ``None`` when absent."""
    return (
        await db.execute(select(QuickReply).where(QuickReply.id == int(reply_id)))
    ).scalar_one_or_none()


async def _shortcut_taken(
    db: AsyncSession,
    *,
    shortcut: str,
    exclude_id: int | None = None,
) -> bool:
    """``True`` when another row already owns ``shortcut``.

    Uses a ``SELECT ... LIMIT 1`` over the unique index instead of relying on the
    ``IntegrityError`` alone, so the common case produces a clean ``409`` with a
    useful message and only the racy case needs the exception path.
    """
    stmt = select(QuickReply.id).where(QuickReply.shortcut == shortcut).limit(1)
    if exclude_id is not None:
        stmt = stmt.where(QuickReply.id != int(exclude_id))
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _all_replies(db: AsyncSession) -> list[QuickReply]:
    """Every reply including retired ones, in display order."""
    return list(
        (
            await db.execute(
                select(QuickReply).order_by(QuickReply.sort_order.asc(), QuickReply.id.asc())
            )
        )
        .scalars()
        .unique()
        .all()
    )


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

        await publish(QUICK_REPLIES_EVENT_CHANNEL, envelope(event_type, payload))
    except Exception as exc:  # noqa: BLE001 - multi-process fan-out is optional
        logger.debug("publish of %s skipped: %s: %s", event_type, type(exc).__name__, exc)


async def _publish_list(db: AsyncSession) -> QuickReplyListOut:
    """Broadcast ``QUICK_REPLIES_UPDATED`` with the full list and return it.

    The list is read from the database **after** the commit, so every client
    receives exactly the committed state instead of a locally assembled guess.
    """
    snapshot = QuickReplyListOut.from_orm_replies(await _all_replies(db))
    await _broadcast(
        EVT_QUICK_REPLIES_UPDATED,
        {"items": [dump_model(item) for item in snapshot.items]},
    )
    return snapshot


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def quick_replies_payload(replies: list[QuickReply]) -> dict[str, Any]:
    """``{"items": [...]}`` — the §5.3 event payload for ``replies``.

    Exposed for callers that already hold the rows (the seed, the router's
    ``GET``) so they never build the envelope shape by hand.
    """
    return {"items": [dump_model(item) for item in QuickReplyListOut.from_orm_replies(replies).items]}


async def list_quick_replies(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[QuickReply]:
    """Return the reply catalogue ordered by ``(sort_order, id)``.

    ``include_inactive=False`` (the operator view, ``GET /api/quick-replies``)
    returns only live replies; ``True`` (``?all=true``, administrator only)
    additionally returns retired ones so they can be reactivated or audited.

    Ordering is done in SQL, and the author relationship is ``selectin``-loaded,
    so the whole list costs two round trips no matter how long it is.
    """
    stmt = select(QuickReply)
    if not include_inactive:
        stmt = stmt.where(QuickReply.is_active.is_(True))
    stmt = stmt.order_by(QuickReply.sort_order.asc(), QuickReply.id.asc())
    return list((await db.execute(stmt)).scalars().unique().all())


async def get_quick_reply(db: AsyncSession, *, reply_id: int) -> QuickReply:
    """One reply by id.

    Raises :class:`QuickReplyNotFoundError` (``404``) when it does not exist and
    :class:`QuickReplyValidationError` (``422``) for an unusable id.
    """
    try:
        key = _reply_key(reply_id)
    except ValueError as exc:
        raise QuickReplyValidationError(str(exc)) from exc
    reply = await _load_reply(db, key)
    if reply is None:
        raise QuickReplyNotFoundError(key)
    return reply


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_quick_reply(
    db: AsyncSession,
    *,
    payload: QuickReplyIn,
    operator: Operator | None,
) -> QuickReply:
    """Insert a new reply authored by ``operator``.

    * :class:`QuickReplyConflictError` — ``shortcut`` is already taken (``409``);
    * :class:`QuickReplyValidationError` — a field broke a §5.1/§5.2 domain rule
      that the schema layer does not cover (``422``).

    The acting operator's id is stored in ``created_by_id`` (``NULL`` when the
    caller has no id). Publishes ``QUICK_REPLIES_UPDATED`` after the commit.
    """
    fields = _as_changes(payload)
    try:
        shortcut = normalise_quick_reply_shortcut(fields.get("shortcut"))
        title = normalise_quick_reply_title(fields.get("title"))
        body = normalise_quick_reply_body(fields.get("body"))
        category = normalise_quick_reply_category(fields.get("category"))
    except ValueError as exc:
        raise QuickReplyValidationError(str(exc)) from exc

    if await _shortcut_taken(db, shortcut=shortcut):
        raise QuickReplyConflictError(shortcut)

    now = utcnow()
    reply = QuickReply(
        shortcut=shortcut,
        title=title,
        body=body,
        category=category,
        sort_order=int(fields.get("sort_order", 0) or 0),
        is_active=bool(fields.get("is_active", True)),
        created_by_id=_operator_id(operator),
        created_at=now,
        updated_at=now,
    )
    db.add(reply)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Lost a race against a concurrent POST of the same shortcut: the unique
        # index is the authority, so report the same 409 the pre-check would have.
        await db.rollback()
        if await _shortcut_taken(db, shortcut=shortcut):
            raise QuickReplyConflictError(shortcut) from exc
        logger.error("insert of quick reply %r failed: %s", shortcut, exc)
        raise

    await db.refresh(reply)
    snapshot = await _publish_list(db)
    logger.info(
        "operator %s created quick reply %s (%r, %d item(s) live)",
        getattr(operator, "username", None) or getattr(operator, "id", None) or "seed",
        reply.id,
        shortcut,
        len(snapshot.items),
    )
    return reply


async def update_quick_reply(
    db: AsyncSession,
    *,
    reply_id: int,
    payload: QuickReplyUpdate,
    operator: Operator | None,
) -> QuickReply:
    """Apply a partial update (``PATCH``) to ``reply_id``.

    Only the fields present in ``payload`` are touched. ``created_by_id`` is never
    rewritten — editing a reply must not steal its authorship.

    * :class:`QuickReplyNotFoundError` — no such reply (``404``);
    * :class:`QuickReplyConflictError` — the new ``shortcut`` belongs to another
      reply (``409``; also raised when a concurrent writer wins the race);
    * :class:`QuickReplyValidationError` — a field broke a domain rule (``422``).

    Publishes ``QUICK_REPLIES_UPDATED`` after the commit.
    """
    try:
        key = _reply_key(reply_id)
    except ValueError as exc:
        raise QuickReplyValidationError(str(exc)) from exc
    changes = _as_changes(payload)
    if not changes:
        raise QuickReplyValidationError("Не передано ни одного поля для изменения")

    reply = await _load_reply(db, key)
    if reply is None:
        raise QuickReplyNotFoundError(key)

    try:
        if "shortcut" in changes:
            shortcut = normalise_quick_reply_shortcut(changes["shortcut"])
            if shortcut != reply.shortcut and await _shortcut_taken(
                db, shortcut=shortcut, exclude_id=key
            ):
                raise QuickReplyConflictError(shortcut)
            reply.shortcut = shortcut
        if "title" in changes:
            reply.title = normalise_quick_reply_title(changes["title"])
        if "body" in changes:
            reply.body = normalise_quick_reply_body(changes["body"])
        if "category" in changes:
            reply.category = normalise_quick_reply_category(changes["category"])
    except ValueError as exc:
        raise QuickReplyValidationError(str(exc)) from exc

    if "sort_order" in changes:
        reply.sort_order = int(changes["sort_order"])
    if "is_active" in changes:
        reply.is_active = bool(changes["is_active"])
    # ``TimestampMixin`` only fires ``onupdate`` when SQLAlchemy sees a dirty column;
    # touching ``updated_at`` explicitly keeps a pure no-op PATCH honest as well.
    reply.updated_at = utcnow()

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        current = await _load_reply(db, key)
        if current is None:
            raise QuickReplyNotFoundError(key) from exc
        if await _shortcut_taken(db, shortcut=str(changes.get("shortcut", "")), exclude_id=key):
            raise QuickReplyConflictError(str(changes.get("shortcut", ""))) from exc
        logger.error("update of quick reply %s failed: %s", key, exc)
        raise

    await db.refresh(reply)
    await _publish_list(db)
    logger.info(
        "operator %s updated quick reply %s (fields=%s)",
        getattr(operator, "username", None) or getattr(operator, "id", None) or "system",
        key,
        ", ".join(sorted(changes)),
    )
    return reply


async def delete_quick_reply(
    db: AsyncSession,
    *,
    reply_id: int,
    operator: Operator | None,
) -> None:
    """Hard-delete ``reply_id``.

    Raises :class:`QuickReplyNotFoundError` (``404``) when it does not exist —
    the "already deleted" case, which makes a retried ``DELETE`` a ``404`` as
    §5.2 requires — and :class:`QuickReplyValidationError` (``422``) for an
    unusable id. Publishes ``QUICK_REPLIES_UPDATED`` after the commit.
    """
    try:
        key = _reply_key(reply_id)
    except ValueError as exc:
        raise QuickReplyValidationError(str(exc)) from exc

    reply = await _load_reply(db, key)
    if reply is None:
        raise QuickReplyNotFoundError(key)

    shortcut = str(reply.shortcut)
    await db.delete(reply)
    await db.commit()

    await _publish_list(db)
    logger.info(
        "operator %s deleted quick reply %s (%r)",
        getattr(operator, "username", None) or getattr(operator, "id", None) or "system",
        key,
        shortcut,
    )

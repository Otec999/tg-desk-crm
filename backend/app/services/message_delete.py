"""Deleting a message from the CRM — the outbound half of CONTRACTS_V5 §1.

Direction
---------
Deletion is initiated **only** from the CRM (§1.1). The opposite direction
(Telegram -> CRM) is :func:`app.services.tg_ingest.handle_deleted` and is not touched
here; it writes the very same two columns.

Invariant "nothing is ever lost" (§1.2)
---------------------------------------
The ``group_messages`` row is **never** deleted physically. A CRM deletion writes

* ``is_deleted_in_tg = True``
* ``deleted_at      = <moment of deletion>``
* ``deleted_source  = 'CRM'``          (new in V5)

``deleted_source`` is written whenever the column exists. The column is owned by
another agent (§7) and is absent from some builds, so the write is guarded by
:func:`supports_deleted_source`: on such a build the soft delete still commits with
the two legacy columns instead of crashing the endpoint with ``AttributeError``. The
column is re-read through a fresh statement (never an ``expire_on_commit`` lazy load),
so no ``MissingGreenlet`` can be raised from a response serialiser.

Ordering (why the CRM write wins)
---------------------------------
1. ``SELECT ... FOR UPDATE`` on the message row -> a concurrent DELETE of the same
   message waits, so exactly one of the two requests passes the ``409`` gate.
2. Idempotency/permission gates.
3. Apply the soft delete and ``commit()`` — **before** anything is sent to Telegram.
   The CRM state therefore changes even if Redis is down (§8 invariant 3).
4. Best-effort publish of ``tg_delete_out`` when the message has both a ``tg_msg_id``
   and its chat has a ``tg_chat_id``; every delivery problem is reported in
   ``telegram.{"requested","delivered","error"}`` instead of raising.
5. ``MESSAGE_DELETED`` fan-out over the existing WebSocket machinery
   (:class:`~app.schemas.WsMessageDeletedData`, ``EVT_MESSAGE_DELETED``), so clients
   repaint the bubble without a reload.

Errors are domain-level (:class:`MessageNotFoundError`, :class:`MessageAlreadyDeletedError`,
:class:`MessageDeleteForbiddenError`); the router translates them into ``404`` / ``409`` / ``403``.
Database-agnostic: every statement below works on PostgreSQL (production) and SQLite (tests).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import DELETED_SOURCE_CRM, DELETED_SOURCE_TELEGRAM, GroupChat, GroupMessage, Operator
from app.schemas import (
    DeletedMessageOut,
    TelegramDeliveryInfo,
    WsMessageDeletedData,
    dump_model,
)
from app.utils import utcnow
from app.ws_manager import EVT_MESSAGE_DELETED

logger = logging.getLogger("tgdesk.message_delete")

__all__ = [
    "DELETE_SOURCE_CRM",
    "EVT_MESSAGE_DELETED",
    "MessageAlreadyDeletedError",
    "MessageDeleteError",
    "MessageDeleteForbiddenError",
    "MessageNotFoundError",
    "build_delete_command",
    "delete_message",
    "publish_delete_to_telegram",
    "supports_deleted_source",
]

#: Value stored in ``group_messages.deleted_source`` for a CRM-initiated deletion.
#: The canonical constant lives in :mod:`app.models` (owned by the models agent, §7),
#: so this module never invents its own spelling of the frozen vocabulary.
DELETE_SOURCE_CRM: Final[str] = DELETED_SOURCE_CRM

#: Redis channel of the delete command (backend -> worker), CONTRACTS_V5 §1.4.
#: Imported lazily inside the publish path so a missing/renamed constant in a peer
#: build degrades to ``telegram.delivered = False`` instead of breaking the import.
_DELETE_CHANNEL_NAME: Final[str] = "tg_delete_out"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MessageDeleteError(Exception):
    """Base class for every domain error raised by this service."""


class MessageNotFoundError(MessageDeleteError):
    """The target message does not exist (the router answers ``404``)."""

    def __init__(self, message_id: Any) -> None:
        self.message_id = message_id
        super().__init__(f"Сообщение #{message_id} не найдено")


class MessageAlreadyDeletedError(MessageDeleteError):
    """The message is already soft-deleted (the router answers ``409``)."""

    def __init__(self, message_id: Any, *, source: str | None = None) -> None:
        self.message_id = message_id
        self.source = source
        where = "в Telegram" if source == DELETED_SOURCE_TELEGRAM else "из CRM"
        super().__init__(f"Сообщение #{message_id} уже удалено ({where})")


class MessageDeleteForbiddenError(MessageDeleteError):
    """The operator lacks ``can_delete_messages`` (the router answers ``403``)."""

    def __init__(self, operator: Any) -> None:
        # Reading attributes off a detached/expired ORM instance would trigger a lazy
        # refresh; outside a greenlet context that raises ``MissingGreenlet`` and masks
        # the 403 as a 500. ``__dict__`` reads the already-loaded values without I/O.
        state = getattr(operator, "__dict__", {}) or {}
        self.operator_id = state.get("id")
        self.username = state.get("username")
        super().__init__("Недостаточно прав: удаление сообщений запрещено")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int | None:
    """Best-effort integer coercion; ``bool`` and garbage yield ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message_key(message_id: Any) -> int:
    """Validate a message id, raising :class:`MessageNotFoundError` when unusable."""
    value = _to_int(message_id)
    if value is None or value <= 0:
        raise MessageNotFoundError(value if value is not None else message_id)
    return value


def supports_deleted_source() -> bool:
    """``True`` when this build's ``GroupMessage`` carries the V5 ``deleted_source`` column.

    The column belongs to the model agent (§7); the soft delete must still work (with
    the two legacy columns) on a build where it has not landed yet, and start writing
    the source automatically once it has.
    """
    return hasattr(GroupMessage, "deleted_source")


def _operator_outranks(operator: Any) -> bool:
    """``True`` for an admin/supervisor (mirrors ``routers.messages._operator_outranks``).

    The router reuses its own helper for the HTTP gate; this one only feeds the
    defence-in-depth check inside the service, so it costs no query (both flags are
    plain properties of the already-loaded row).
    """
    return bool(getattr(operator, "can_delete_messages", False)) or bool(
        getattr(operator, "is_privileged", False)
    )


async def _load_message(db: AsyncSession, message_id: int) -> GroupMessage | None:
    """Return the message row with the relationships the response needs.

    ``FOR UPDATE`` serialises two concurrent ``DELETE``s of one message, and the two
    ``selectinload`` collections let :class:`~app.schemas.MessageOut` be built after the
    commit without a lazy load (which would raise ``MissingGreenlet``).
    """
    statement = (
        select(GroupMessage)
        .where(GroupMessage.id == int(message_id))
        .options(
            selectinload(GroupMessage.attachments),
            selectinload(GroupMessage.reactions),
            selectinload(GroupMessage.edits),
        )
        .with_for_update(of=GroupMessage)
    )
    return (await db.execute(statement)).scalars().first()


# ---------------------------------------------------------------------------
# Telegram command (best effort, CONTRACTS_V5 §1.4)
# ---------------------------------------------------------------------------


def build_delete_command(
    message: GroupMessage,
    chat: GroupChat | None,
    operator: Any,
) -> dict[str, Any]:
    """Build the ``tg_delete_out`` payload exactly as frozen by §1.4.

    ``{cmd_id, message_id, chat_id, tg_chat_id, tg_msg_id, operator_username}`` — no
    extra keys, so the worker's strict validation cannot be surprised by this side.
    """
    return {
        "cmd_id": str(uuid.uuid4()),
        "message_id": int(message.id),
        "chat_id": int(message.chat_id),
        "tg_chat_id": _to_int(getattr(chat, "tg_chat_id", None)),
        "tg_msg_id": _to_int(getattr(message, "tg_msg_id", None)),
        "operator_username": getattr(operator, "username", None),
    }


async def publish_delete_to_telegram(
    db: AsyncSession,
    *,
    message: GroupMessage,
    operator: Any,
) -> TelegramDeliveryInfo:
    """Publish the §1.4 delete command; **never** raises.

    Returns :class:`~app.schemas.TelegramDeliveryInfo`:

    * ``requested=False`` — the message has no ``tg_msg_id`` or its chat has no
      ``tg_chat_id`` (a mock chat, or a row that never reached Telegram). That is a
      normal state: nothing to delete, and not an error;
    * ``delivered=True`` — the command reached at least one Redis subscriber;
    * ``error`` — the failure text when the lookup or the publish failed.
    """
    chat: GroupChat | None = None
    try:
        message_tg_id = _to_int(getattr(message, "tg_msg_id", None))
        chat_id = _to_int(getattr(message, "chat_id", None))
        if chat_id is None:
            return TelegramDeliveryInfo(
                requested=False, delivered=False, error="у сообщения нет chat_id"
            )
        chat = (
            await db.execute(select(GroupChat).where(GroupChat.id == int(chat_id)))
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - the CRM write is already committed
        logger.warning(
            "delete command lookup failed for message %s: %s: %s",
            getattr(message, "id", "?"),
            type(exc).__name__,
            exc,
        )
        return TelegramDeliveryInfo(requested=False, delivered=False, error=str(exc))

    tg_chat_id = _to_int(getattr(chat, "tg_chat_id", None)) if chat is not None else None
    if message_tg_id is None or tg_chat_id is None:
        logger.debug(
            "message %s has no Telegram counterpart (tg_msg_id=%s, tg_chat_id=%s); "
            "deletion stays CRM-only",
            getattr(message, "id", "?"),
            message_tg_id,
            tg_chat_id,
        )
        return TelegramDeliveryInfo(requested=False, delivered=False, error=None)

    payload = build_delete_command(message, chat, operator)
    try:
        from app.redis_bus import publish

        try:
            from app.redis_bus import CH_TG_DELETE_OUT

            channel = CH_TG_DELETE_OUT
        except ImportError:  # pragma: no cover - peer build without the constant
            channel = _DELETE_CHANNEL_NAME

        receivers = int(await publish(channel, payload) or 0)
    except Exception as exc:  # noqa: BLE001 - REST must still answer 200 (§1.3)
        logger.warning(
            "could not queue the deletion of message %s for Telegram: %s: %s",
            message.id,
            type(exc).__name__,
            exc,
        )
        return TelegramDeliveryInfo(requested=True, delivered=False, error=str(exc))

    # ``publish`` returns 0 both for "no subscriber" and for a swallowed transport
    # error. Reporting ``delivered=False`` in the second case is honest, and keeping
    # ``requested=True`` tells the client a command was produced.
    delivered = receivers > 0
    logger.info(
        "deletion of message %s queued for Telegram (cmd_id=%s, receivers=%d)",
        message.id,
        payload["cmd_id"],
        receivers,
    )
    return TelegramDeliveryInfo(
        requested=True,
        delivered=delivered,
        error=None if delivered else "команда удаления не доставлена ни одному воркеру",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def delete_message(
    db: AsyncSession,
    *,
    message_id: int,
    operator: Operator,
) -> DeletedMessageOut:
    """Soft-delete a message from the CRM and mirror the deletion to Telegram.

    1. load the row ``FOR UPDATE`` — absent -> :class:`MessageNotFoundError` (404);
    2. already deleted -> :class:`MessageAlreadyDeletedError` (409);
    3. missing ``can_delete_messages``/privilege -> :class:`MessageDeleteForbiddenError`
       (403);
    4. write ``is_deleted_in_tg``/``deleted_at``/``deleted_source='CRM'`` and commit;
    5. best-effort ``tg_delete_out`` (never raises, see
       :func:`publish_delete_to_telegram`);
    6. broadcast ``MESSAGE_DELETED`` with :class:`~app.schemas.WsMessageDeletedData`;
    7. return :class:`~app.schemas.DeletedMessageOut`.

    The Telegram step runs **after** the commit, so an unreachable worker can never
    roll back the CRM-side deletion (§8 invariant 3).
    """
    key = _message_key(message_id)

    message = await _load_message(db, key)
    if message is None:
        logger.warning("message %s not found; deletion refused", key)
        await db.rollback()
        raise MessageNotFoundError(key)

    if bool(getattr(message, "is_deleted_in_tg", False)):
        previous = getattr(message, "deleted_source", None)
        logger.info("message %s is already deleted (source=%s); 409", key, previous)
        await db.rollback()
        raise MessageAlreadyDeletedError(key, source=previous)

    if not _operator_outranks(operator) and not bool(
        getattr(operator, "can_delete_messages", False)
    ):
        logger.warning(
            "operator %s (id=%s) may not delete message %s: can_delete_messages is False",
            getattr(operator, "username", "?"),
            getattr(operator, "id", "?"),
            key,
        )
        await db.rollback()
        raise MessageDeleteForbiddenError(operator)

    now = utcnow()
    message.is_deleted_in_tg = True
    message.deleted_at = now
    wrote_source = supports_deleted_source()
    if wrote_source:
        # §1.2: the flag and its source must never drift apart (the DB CHECK enforces
        # the same pairing, so a mismatch here would surface as an IntegrityError).
        message.deleted_source = DELETE_SOURCE_CRM
    else:
        logger.warning(
            "GroupMessage.deleted_source is missing in this build; message %s is "
            "soft-deleted with is_deleted_in_tg/deleted_at only",
            key,
        )

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - surface as a clean error, session stays usable
        await db.rollback()
        logger.exception("could not commit the deletion of message %s", key)
        raise MessageDeleteError(
            f"Не удалось сохранить удаление сообщения #{key}: {type(exc).__name__}"
        ) from exc

    chat_id = int(getattr(message, "chat_id", 0) or 0)

    telegram = await publish_delete_to_telegram(db, message=message, operator=operator)

    # A ``refresh`` would expire the selectin collections and lose media/reactions in the
    # response, so the committed row is re-read with a fresh statement instead.
    try:
        refreshed = await _load_message(db, key)
        if refreshed is not None:
            message = refreshed
    except Exception as exc:  # noqa: BLE001 - the in-memory row is still correct
        logger.warning("could not re-read message %s after commit: %s", key, exc)

    deleted_at = getattr(message, "deleted_at", None) or now
    source = getattr(message, "deleted_source", None)
    if source is None:
        source = DELETE_SOURCE_CRM

    try:
        data = dump_model(WsMessageDeletedData.from_orm_message(message))
    except Exception as exc:  # noqa: BLE001 - fan-out must not fail the request
        logger.warning(
            "could not build MESSAGE_DELETED data for message %s: %s: %s",
            key,
            type(exc).__name__,
            exc,
        )
        data = {"message_id": key, "chat_id": chat_id, "deleted_at": None, "message": None}

    try:
        from app.routers._common import safe_broadcast

        await safe_broadcast(EVT_MESSAGE_DELETED, data)
    except Exception as exc:  # noqa: BLE001 - a dead socket must not undo the deletion
        logger.warning(
            "broadcast of %s for message %s failed: %s: %s",
            EVT_MESSAGE_DELETED,
            key,
            type(exc).__name__,
            exc,
        )

    message_payload = data.get("message")
    if message_payload is None:
        message_payload = _message_fallback_payload(message)

    result = DeletedMessageOut(
        message_id=key,
        chat_id=chat_id,
        deleted_at=deleted_at,
        deleted_source=str(source),
        message=message_payload,
        telegram=telegram,
    )
    logger.info(
        "operator %s deleted message %s in chat %s (telegram requested=%s delivered=%s)",
        getattr(operator, "username", "?"),
        key,
        chat_id,
        telegram.requested,
        telegram.delivered,
    )
    return result


def _message_fallback_payload(message: GroupMessage) -> Any:
    """Build ``MessageOut`` when the shared WS builder could not produce it."""
    from app.routers._common import message_out  # noqa: PLC0415 - peer module

    try:
        return message_out(message)
    except Exception as exc:  # noqa: BLE001 - last resort, keep the contract shape
        logger.warning(
            "could not serialise message %s for the delete response: %s: %s",
            getattr(message, "id", "?"),
            type(exc).__name__,
            exc,
        )
        from app.schemas import MessageOut  # noqa: PLC0415

        return MessageOut.from_orm_message(message)

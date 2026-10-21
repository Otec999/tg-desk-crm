"""Messages router (§3.4): operator -> Telegram group message send.

Send pipeline (each step is a hard gate):

1. resolve the chat (``404`` when unknown);
2. ``client_nonce`` idempotency - a replay returns the original message (``200``);
3. typing-lock collision - another operator holds the lock -> ``409``;
4. ``reply_to_msg_id`` resolution (CRM id first, then TG msg id);
5. sanitise the text - empty after sanitising -> ``422``;
6. persist as ``delivery_status="pending"``, bump the chat and clear unread;
7. ``sla_engine.resolve_for_reply`` (flush-only; this router owns the commit);
8. publish ``tg_outgoing`` on Redis (best effort - never fails the request);
9. broadcast ``NEW_MESSAGE`` / ``SLA_RESOLVED`` **after** the commit;
10. return ``201 MessageOut``.

Mock chats (``tg_chat_id`` is ``NULL``) have no Telegram counterpart, so their
messages are marked ``sent`` immediately while still being broadcast.

``SessionLocal`` runs with ``autoflush=False``, so every step that must be
visible to a helper query issues an explicit ``flush()``.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.enums import EditSource
from app.models import DeliveryStatus, GroupMessage, MessageSenderType, Operator
from app.routers._common import (
    as_contract,
    iso_or_none,
    message_out,
    safe_broadcast_many,
    sqlstate_of,
    to_contract_dict,
)
from app.routers.chats import get_chat, load_chat_list_item, operator_display_names
from app.schemas import (
    DeletedMessageOut,
    ForwardRequest,
    MessageEditListOut,
    MessageEditOut,
    MessageEditRequest,
    MessageOut,
    MessageUpdateOut,
    SendMessageRequest,
)
from app.security import get_current_operator
from app.services import sla_engine
from app.services.media import (
    MAX_FILES_PER_UPLOAD,
    attachment_to_out,
    enforce_upload_request_size,
    resolve_media_path,
    save_upload,
)
from app.utils import extract_phone, sanitize_telegram_text, utcnow

logger = logging.getLogger("app.routers.messages")

#: The object ``main.mount_router`` mounts under ``/api``. It deliberately carries **no**
#: prefix of its own: FastAPI's ``include_router`` prepends the host's prefix to every
#: included route and drops the included router's own prefix, so a prefixed host would
#: flatten ``/messages/...`` into ``/chats/messages/...``. Keeping the host bare and
#: composing two sub-routers below preserves both CONTRACTS_V3 paths.
router = APIRouter(tags=["messages"])

#: ``/api/chats/{chat_id}/...`` — sending a message or an attachment (CONTRACTS.md §3.4).
chats_router = APIRouter(prefix="/chats", tags=["messages"])

#: ``/api/messages/{message_id}/...`` — editing and forwarding (CONTRACTS_V3 §1.2/§2.3).
message_router = APIRouter(prefix="/messages", tags=["messages"])

#: Redis channel consumed by the Telethon worker (§5).
OUTGOING_CHANNEL = "tg_outgoing"

#: Hard cap enforced by Telegram (and by the DB CHECK constraint).
MAX_TEXT_LENGTH = 4096


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _normalise_nonce(raw: str | None) -> str | None:
    """Trim a client nonce and treat blanks as absent."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned[:64] or None


async def _existing_by_nonce(db: AsyncSession, nonce: str) -> GroupMessage | None:
    """Return the message previously created for ``nonce``.

    ``group_messages.client_nonce`` is globally unique (§6), so the lookup is
    not scoped to a chat: a replay always maps back to the original row.
    """
    return (
        await db.execute(select(GroupMessage).where(GroupMessage.client_nonce == nonce))
    ).scalar_one_or_none()


async def _resolve_reply(db: AsyncSession, chat_id: int, reply_to_msg_id: int | None) -> GroupMessage | None:
    """Resolve ``reply_to_msg_id`` as a CRM id first, then as a TG msg id."""
    if reply_to_msg_id is None:
        return None
    value = int(reply_to_msg_id)
    target = (
        await db.execute(
            select(GroupMessage).where(GroupMessage.chat_id == chat_id, GroupMessage.id == value)
        )
    ).scalar_one_or_none()
    if target is not None:
        return target
    if value <= 0:
        return None
    return (
        await db.execute(
            select(GroupMessage)
            .where(GroupMessage.chat_id == chat_id, GroupMessage.tg_msg_id == value)
            .order_by(GroupMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _acquire_send_guard(chat_id: int, operator: Operator, db: AsyncSession) -> None:
    """Raise ``409`` when another operator holds the chat's typing lock.

    ``redis_bus.get_typing_lock`` returns the owner's operator id — or ``None``
    when the chat is free *or* Redis is unreachable. A degraded Redis must never
    block the desk, so an unreadable lock is treated as free.
    """
    try:
        from app.redis_bus import get_typing_lock

        owner_id = await get_typing_lock(chat_id)
    except Exception as exc:  # noqa: BLE001 - Redis down must not block sending
        logger.warning("typing lock check failed for chat %s: %s", chat_id, exc)
        return

    if owner_id is None or int(owner_id) == int(operator.id):
        return

    locked_by = (await operator_display_names(db, {int(owner_id)})).get(int(owner_id)) or (
        f"Operator #{int(owner_id)}"
    )
    logger.info("chat %s locked by operator %s; %s refused", chat_id, owner_id, operator.username)
    # §3.4: the body carries ``locked_by`` so the frontend can name the holder.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"detail": "Chat is locked by another operator", "locked_by": locked_by},
        headers={"X-Locked-By": locked_by},
    )


async def _release_typing_lock(chat_id: int, operator_id: int) -> None:
    """Release our own typing lock after a successful send (compare-and-delete)."""
    try:
        from app.services.typing import release_chat_lock

        await release_chat_lock(chat_id, operator_id)
    except Exception as exc:  # noqa: BLE001 - the 15 s TTL is the safety net
        logger.warning("could not release typing lock for chat %s: %s", chat_id, exc)


async def _publish_outgoing(
    chat: Any,
    message: GroupMessage,
    operator: Operator,
    reply_to_tg_msg_id: int | None,
    attachment_path: str | None = None,
) -> str:
    """Publish the §5 ``tg_outgoing`` command; returns the ``cmd_id`` used.

    ``attachment_path`` (CONTRACTS_V2 §5) carries an absolute on-disk path; the worker
    switches to ``send_file`` when it is present and falls back to ``send_message``
    otherwise.
    """
    cmd_id = str(uuid.uuid4())
    payload = {
        "cmd_id": cmd_id,
        "chat_id": int(chat.id),
        "tg_chat_id": chat.tg_chat_id,
        "message_id": int(message.id),
        "text": message.text,
        "reply_to_tg_msg_id": reply_to_tg_msg_id,
        "operator_username": operator.username,
        "via_crm": True,
    }
    if attachment_path:
        payload["attachment_path"] = attachment_path
    try:
        from app.redis_bus import publish

        await publish(OUTGOING_CHANNEL, payload)
    except Exception as exc:  # noqa: BLE001 - the REST call must still succeed
        logger.warning(
            "failed to publish %s for message %s (chat %s): %s: %s",
            OUTGOING_CHANNEL,
            message.id,
            chat.id,
            type(exc).__name__,
            exc,
        )
    return cmd_id


async def _discard_uploads(attachments: list[Any]) -> None:
    """Delete the on-disk files *and now-empty folders* of a failed upload batch.

    ``attachments`` must be snapshots produced by :func:`_attachment_snapshot`, **not**
    live ORM rows: the caller rolls the transaction back before cleaning up, and a
    rollback expires every attribute, so touching ``row.storage_path`` afterwards
    would trigger a lazy refresh — synchronously, from a pure file utility — and blow
    up with ``MissingGreenlet`` (which used to turn the 413 into a 500 and leave the
    orphans behind).
    """
    from app.services.media import delete_attachment_files, messages_media_root  # noqa: PLC0415

    for snapshot in attachments:
        try:
            await delete_attachment_files(snapshot)
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the real error
            logger.warning(
                "could not remove the files of attachment %s: %s: %s",
                getattr(snapshot, "id", "?"),
                type(exc).__name__,
                exc,
            )
    prune_message_folders(attachments, keep_root=messages_media_root())


def prune_message_folders(attachments: list[Any], *, keep_root: Any) -> None:
    """Remove the ``chats/{chat_id}/{message_id}`` folders a discarded batch created.

    ``save_upload`` prunes its own folder, but the *earlier*, fully-committed files of
    the batch kept theirs alive; after their files are gone those directories are
    litter. The message row was rolled back, so no future upload can reuse them.
    ``keep_root`` (``media_root/chats``) is never removed, and a non-empty folder is
    left alone — the pruning stops on the first ``rmdir`` that fails.
    """
    for snapshot in attachments:
        relative = getattr(snapshot, "storage_path", None)
        if not relative:
            continue
        try:
            folder = resolve_media_path(relative).parent
        except (ValueError, OSError):
            continue
        for candidate in (folder, folder.parent):
            try:
                if candidate == keep_root or keep_root not in candidate.parents:
                    break
                candidate.rmdir()
            except OSError:
                break


def _attachment_snapshot(attachment: Any) -> SimpleNamespace:
    """Freeze the attributes cleanup needs, so a later rollback cannot expire them."""
    return SimpleNamespace(
        id=getattr(attachment, "id", None),
        storage_path=getattr(attachment, "storage_path", None),
        thumbnail_path=getattr(attachment, "thumbnail_path", None),
    )


def _sla_resolved_data(incident: Any, stats: Any, operator_id: int) -> dict[str, Any]:
    """Build the §4 ``SLA_RESOLVED`` payload (``stats`` is a plain dict)."""
    return {
        "chat_id": int(getattr(incident, "chat_id", 0)),
        "incident_id": int(getattr(incident, "id", 0)),
        "operator_id": int(operator_id),
        "response_time_seconds": getattr(incident, "response_time_seconds", None),
        "stats": dict(stats) if isinstance(stats, dict) else None,
    }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@chats_router.post(
    "/{chat_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message from an operator into the Telegram group chat",
    responses={
        200: {"description": "Idempotent replay of a message with the same client_nonce"},
        404: {"description": "Unknown chat"},
        409: {"description": "Chat is locked by another operator"},
        422: {"description": "Message text is empty after sanitising"},
    },
)
async def send_message(
    chat_id: int,
    payload: SendMessageRequest,
    response: Response,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageOut:
    """Create the outbound message, resolve the SLA incident and notify everyone."""
    chat = await get_chat(db, chat_id)

    nonce = _normalise_nonce(getattr(payload, "client_nonce", None))
    if nonce is not None:
        replayed = await _existing_by_nonce(db, nonce)
        if replayed is not None:
            logger.info("idempotent replay of nonce %s (message %s)", nonce, replayed.id)
            # Contract §3.4: a replay is a plain ``200`` with the original message.
            response.status_code = status.HTTP_200_OK
            return message_out(replayed)

    await _acquire_send_guard(chat_id, operator, db)

    text = sanitize_telegram_text(payload.text or "")
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message text is empty after sanitising",
        )
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Message text exceeds {MAX_TEXT_LENGTH} characters",
        )

    reply_target = await _resolve_reply(db, chat_id, getattr(payload, "reply_to_msg_id", None))

    now = utcnow()
    is_mock_chat = chat.tg_chat_id is None
    message = GroupMessage(
        chat_id=chat.id,
        tg_msg_id=None,
        sender_type=MessageSenderType.OPERATOR.value,
        sender_operator_id=operator.id,
        sender_name=operator.display_name,
        sender_tg_id=None,
        text=text,
        reply_to_id=int(reply_target.id) if reply_target is not None else None,
        reply_to_tg_msg_id=(
            int(reply_target.tg_msg_id)
            if reply_target is not None and reply_target.tg_msg_id is not None
            else None
        ),
        phone=extract_phone(text),
        is_deleted_in_tg=False,
        deleted_at=None,
        sla_incident_id=None,
        delivery_status=(DeliveryStatus.SENT.value if is_mock_chat else DeliveryStatus.PENDING.value),
        client_nonce=nonce,
        created_at=now,
    )
    db.add(message)
    chat.last_message_at = now
    chat.unread_count = 0

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if sqlstate_of(exc) == "23505" and nonce is not None:
            raced = await _existing_by_nonce(db, nonce)
            if raced is not None:
                logger.info("idempotency race resolved for nonce %s (message %s)", nonce, raced.id)
                return message_out(raced)
        logger.exception("could not insert message into chat %s", chat_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Message could not be stored (duplicate client_nonce)",
        ) from exc

    incident = await sla_engine.resolve_for_reply(db, chat_id, message.reply_to_tg_msg_id, operator, message)
    if incident is not None:
        message.sla_incident_id = incident.id

    stats: dict[str, Any] | None = None
    if incident is not None:
        try:
            stats = await sla_engine.personal_stats(db, operator)
        except Exception as exc:  # noqa: BLE001 - stats are decorative in this event
            logger.warning("personal_stats failed while resolving incident %s: %s", incident.id, exc)

    await db.commit()
    await db.refresh(message)
    if incident is not None:
        await db.refresh(incident)

    if not is_mock_chat:
        await _publish_outgoing(chat, message, operator, message.reply_to_tg_msg_id)
    await _release_typing_lock(chat_id, int(operator.id))

    message_payload = to_contract_dict("message", message)
    chat_item = await load_chat_list_item(db, chat_id)
    chat_payload = chat_item if isinstance(chat_item, dict) else chat_item.model_dump(mode="json")

    envelopes: list[tuple[str, Any]] = [("NEW_MESSAGE", {**message_payload, "chat": chat_payload})]
    if incident is not None:
        envelopes.append(("SLA_RESOLVED", _sla_resolved_data(incident, stats, int(operator.id))))
    await safe_broadcast_many(envelopes)

    logger.info(
        "operator %s sent message %s to chat %s (mock=%s, nonce=%s, resolved_incident=%s)",
        operator.username,
        message.id,
        chat_id,
        is_mock_chat,
        nonce,
        getattr(incident, "id", None),
    )
    return message_out(message)


@chats_router.post(
    "/{chat_id}/attachments",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send one or more files from an operator into the Telegram group chat",
    responses={
        404: {"description": "Unknown chat"},
        409: {"description": "Chat is locked by another operator"},
        413: {"description": "File exceeds the upload limit"},
        422: {"description": "No file supplied, or an empty file"},
    },
)
async def send_attachments(
    chat_id: int,
    request: Request,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _size_guard: Annotated[int, Depends(enforce_upload_request_size)],
    files: list[UploadFile] = File(..., description="Файлы для отправки"),
    text: str | None = Form(default=None, description="Необязательная подпись"),
    reply_to_id: int | None = Form(default=None, description="ID сообщения, на которое отвечаем"),
) -> MessageOut:
    """Store the uploads, hand the first file to the worker and broadcast the message.

    One CRM message is created per request: Telegram delivers a multi-file album, but the
    operator reply thread and SLA bookkeeping stay keyed to a single message.

    ``_size_guard`` is a dependency, not a plain call: FastAPI parses ``File``/``Form``
    bodies **before** the handler runs, so only a dependency can refuse an oversized POST
    before Starlette spools it to disk.
    """
    chat = await get_chat(db, chat_id)

    if not files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не выбран ни один файл",
        )
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"За один раз можно отправить не более {MAX_FILES_PER_UPLOAD} файлов",
        )

    await _acquire_send_guard(chat_id, operator, db)

    caption = sanitize_telegram_text(text or "") if text else ""
    if len(caption) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Подпись превышает {MAX_TEXT_LENGTH} символов",
        )

    reply_target = await _resolve_reply(db, chat_id, reply_to_id)

    now = utcnow()
    is_mock_chat = chat.tg_chat_id is None
    message = GroupMessage(
        chat_id=chat.id,
        tg_msg_id=None,
        sender_type=MessageSenderType.OPERATOR.value,
        sender_operator_id=operator.id,
        sender_name=operator.display_name,
        sender_tg_id=None,
        text=caption,
        reply_to_id=int(reply_target.id) if reply_target is not None else None,
        reply_to_tg_msg_id=(
            int(reply_target.tg_msg_id)
            if reply_target is not None and reply_target.tg_msg_id is not None
            else None
        ),
        phone=extract_phone(caption),
        is_deleted_in_tg=False,
        deleted_at=None,
        sla_incident_id=None,
        delivery_status=(DeliveryStatus.SENT.value if is_mock_chat else DeliveryStatus.PENDING.value),
        client_nonce=None,
        created_at=now,
    )
    db.add(message)
    chat.last_message_at = now
    chat.unread_count = 0
    await db.flush()

    attachments: list[Any] = []
    stored: list[Any] = []
    try:
        for upload in files:
            attachment = await save_upload(
                db, chat_id=int(chat.id), message_id=int(message.id), upload=upload
            )
            attachments.append(attachment)
            stored.append(_attachment_snapshot(attachment))
    except HTTPException:
        # ``save_upload`` deletes *its own* half-written file, but every earlier
        # iteration is already committed — drop those too, or a rejected album leaves
        # orphaned bytes on disk that no row points at any more.
        await db.rollback()
        await _discard_uploads(stored)
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a half-written message behind
        await db.rollback()
        await _discard_uploads(stored)
        logger.exception("attachment upload failed for chat %s", chat_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось сохранить файл",
        ) from exc

    # The first attachment defines the message kind so the thread renders correctly.
    if attachments:
        message.kind = getattr(attachments[0], "kind", None) or "DOCUMENT"

    incident = None
    try:
        incident = await sla_engine.resolve_for_reply(
            db, chat_id, message.reply_to_tg_msg_id, operator, message
        )
        if incident is not None:
            message.sla_incident_id = incident.id
        await db.commit()
    except Exception:
        await db.rollback()
        await _discard_uploads(stored)
        raise
    await db.refresh(message)

    if not is_mock_chat and attachments:
        first = attachments[0]
        storage_path = getattr(first, "storage_path", None)
        absolute: str | None = None
        if storage_path:
            try:
                absolute = str(resolve_media_path(storage_path))
            except (ValueError, OSError) as exc:
                logger.warning("attachment %s has an unusable path (%s)", getattr(first, "id", "?"), exc)
        await _publish_outgoing(chat, message, operator, message.reply_to_tg_msg_id, absolute)
    await _release_typing_lock(chat_id, int(operator.id))

    result = message_out(message)
    result.attachments = [attachment_to_out(a) for a in attachments]

    message_payload = to_contract_dict("message", message)
    message_payload["attachments"] = [a.model_dump(mode="json") for a in result.attachments]
    chat_item = await load_chat_list_item(db, chat_id)
    chat_payload = chat_item if isinstance(chat_item, dict) else chat_item.model_dump(mode="json")

    envelopes: list[tuple[str, Any]] = [("NEW_MESSAGE", {**message_payload, "chat": chat_payload})]
    if attachments:
        envelopes.append(
            (
                "MESSAGE_MEDIA",
                {
                    "message_id": int(message.id),
                    "chat_id": int(chat.id),
                    "attachments": [a.model_dump(mode="json") for a in result.attachments],
                },
            )
        )
    if incident is not None:
        stats = None
        try:
            stats = await sla_engine.personal_stats(db, operator)
        except Exception as exc:  # noqa: BLE001 - stats are decorative
            logger.warning("personal_stats failed after attachment send: %s", exc)
        envelopes.append(("SLA_RESOLVED", _sla_resolved_data(incident, stats, int(operator.id))))
    await safe_broadcast_many(envelopes)

    logger.info(
        "operator %s sent %s attachment(s) in message %s to chat %s",
        operator.username,
        len(attachments),
        message.id,
        chat_id,
    )
    return result


# ---------------------------------------------------------------------------
# CONTRACTS_V3 — message editing (§2.3) and forwarding (§1.2)
# ---------------------------------------------------------------------------


def _operator_outranks(operator: Operator) -> bool:
    """True when ``operator`` may edit messages they did not write.

    §2.3: the author may always edit; anybody else needs ``can_delete_messages``
    or a privileged role (admin / supervisor). Both attributes are properties on
    the already-loaded row, so this costs no query.
    """
    return bool(getattr(operator, "can_delete_messages", False)) or bool(
        getattr(operator, "is_privileged", False)
    )


def _can_edit_message(operator: Operator, message: GroupMessage) -> bool:
    """§2.3 permission gate.

    Three cases, in order:

    * **own** message (``sender_operator_id == operator.id``) — always allowed;
    * **inbound** message (client or bot, ``sender_operator_id IS NULL``) — allowed,
      because correcting a typo in a reply addressed to the client is the whole point
      of editing from the CRM. The edit still goes out under our shared Telegram
      account, so no operator can rewrite another operator's words this way;
    * **another operator's** message — needs elevated rights, otherwise one operator
      could silently rewrite a colleague's reply to the client.
    """
    if message.sender_operator_id is not None:
        return int(message.sender_operator_id) == int(operator.id) or _operator_outranks(operator)
    return True


async def _reload_with_relations(db: AsyncSession, message_id: int) -> GroupMessage:
    """Re-read a message with every relationship the response schema needs.

    ``Session.commit()`` expires all loaded attributes. A plain ``refresh()`` then
    reloads only the scalar columns, so the ``selectin`` collections come back empty
    and the response body would claim a photo message has no attachments even though
    the rows exist. An explicit ``select`` with ``selectinload`` is deterministic:
    ``populate_existing`` forces the stale identity-map instance to be overwritten.
    """
    statement = (
        select(GroupMessage)
        .where(GroupMessage.id == int(message_id))
        .options(
            selectinload(GroupMessage.attachments),
            selectinload(GroupMessage.reactions),
            selectinload(GroupMessage.edits),
        )
        .execution_options(populate_existing=True)
    )
    reloaded = (await db.execute(statement)).scalars().first()
    if reloaded is None:  # pragma: no cover - the row exists, it was just committed
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Сообщение #{message_id} не найдено",
        )
    return reloaded


async def _load_message(db: AsyncSession, message_id: int) -> GroupMessage:
    """Load one message by primary key or raise ``404`` with a Russian message."""
    message = await db.get(GroupMessage, int(message_id))
    if message is None:
        logger.warning("message %s not found", message_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Сообщение #{message_id} не найдено",
        )
    return message


def _is_mock_chat(chat: Any) -> bool:
    """True when the chat has no Telegram counterpart (``tg_chat_id IS NULL``)."""
    return getattr(chat, "tg_chat_id", None) is None


def _edit_to_payload(edit: Any) -> dict[str, Any]:
    """Serialise a ``MessageEdit`` row through the contract shape.

    Prefers the service-side ``edit_to_dict`` so every consumer agrees on the JSON
    shape; falls back to :class:`MessageEditOut` when the peer module is absent.
    """
    try:
        from app.services.edits import edit_to_dict  # noqa: PLC0415 - peer module

        payload = edit_to_dict(edit)
        if isinstance(payload, dict):
            return dict(payload)
    except Exception as exc:  # noqa: BLE001 - the schema fallback is enough
        logger.debug("edit_to_dict unavailable (%s); using MessageEditOut", exc)
    try:
        return MessageEditOut.from_orm_edit(edit).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - one bad row must not break the page
        logger.warning("could not serialise edit %s: %s", getattr(edit, "id", "?"), exc)
        return {}


def _first_attachment_command_payload(new_message: GroupMessage) -> tuple[str | None, str | None]:
    """Resolve ``(absolute_path, kind)`` of the first copied attachment (§1.3).

    The rows belong to the session that just committed the forward, so reading
    them is a normal async query. Any failure degrades to a text-only send rather
    than failing the request — the message itself is already stored.
    """
    try:
        from app.services.forwarding import _first_attachment_path  # noqa: PLC0415

        path, kind = _first_attachment_path(new_message)
        return path, kind
    except Exception as exc:  # noqa: BLE001 - the worker falls back to send_message
        logger.warning(
            "could not resolve the copied attachment of message %s: %s", new_message.id, exc
        )
        return None, None


def _message_edited_payload(
    message: GroupMessage,
    *,
    source: str,
    edit_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the §2.6 ``MESSAGE_EDITED`` payload."""
    return {
        "message_id": int(message.id),
        "chat_id": int(message.chat_id),
        "text": message.text or "",
        "edited_at": iso_or_none(getattr(message, "updated_at", None) or utcnow()),
        "source": source,
        "edit": edit_payload,
    }


# ---------------------------------------------------------------------------
# PATCH /api/messages/{message_id} — edit from CRM
# ---------------------------------------------------------------------------


@message_router.patch(
    "/{message_id}",
    response_model=MessageUpdateOut,
    summary="Edit a message from the CRM (mirrored into Telegram)",
    responses={
        403: {"description": "Not the author and no elevated rights"},
        404: {"description": "Unknown message"},
        409: {"description": "The message was deleted in Telegram"},
        422: {"description": "Empty text after sanitising"},
    },
)
async def edit_message(
    message_id: int,
    payload: MessageEditRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageUpdateOut:
    """Apply a CRM edit: update CRM, record no history, mirror to Telegram.

    Asymmetry (§2.1): a CRM edit is our own action, so ``message_edits`` gets **no**
    row — :func:`app.services.edits.apply_crm_edit` owns that decision and the
    router only calls it. Only edits arriving from Telegram are recorded (§5.3).
    """
    from app.services.edits import apply_crm_edit, publish_edit_command  # noqa: PLC0415

    message = await _load_message(db, message_id)
    chat = await get_chat(db, int(message.chat_id))

    if not _can_edit_message(operator, message):
        logger.warning(
            "operator %s (id=%s) may not edit message %s of operator %s",
            operator.username,
            operator.id,
            message_id,
            message.sender_operator_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: редактировать можно только свои сообщения "
            "или нужен флаг «удаление сообщений»",
        )

    # Invariant §5.5: a message that no longer exists in Telegram cannot be edited.
    if bool(getattr(message, "is_deleted_in_tg", False)):
        logger.warning("refusing to edit message %s: deleted in Telegram", message_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сообщение удалено в Telegram — редактирование невозможно",
        )

    text = sanitize_telegram_text(payload.text or "")
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Текст сообщения пуст после очистки",
        )
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Текст сообщения превышает {MAX_TEXT_LENGTH} символов",
        )

    edited = await apply_crm_edit(db, message, new_text=text, operator=operator)

    edited_in_telegram = False
    # Telegram only lets an account edit messages IT sent: asking it to edit a client's
    # message fails with ``MessageAuthorRequiredError`` and would leave the row stuck in
    # ``pending`` forever. For inbound (client/bot) messages the CRM text is therefore
    # corrected locally and nothing is sent — the operator still sees the fix.
    is_ours_in_telegram = (
        getattr(edited, "sender_type", None) == MessageSenderType.OPERATOR.value
        and getattr(edited, "tg_msg_id", None) is not None
        and not _is_mock_chat(chat)
    )
    if not is_ours_in_telegram:
        # Nothing awaits a remote acknowledgement, so settle the delivery state now;
        # otherwise ``apply_crm_edit`` would leave ``pending`` forever.
        edited.delivery_status = DeliveryStatus.SENT.value
    else:
        try:
            await publish_edit_command(edited, chat, operator)
            edited_in_telegram = True
        except Exception as exc:  # noqa: BLE001 - the CRM edit stands even if Redis is down
            logger.warning(
                "could not publish the edit of message %s: %s: %s",
                message_id,
                type(exc).__name__,
                exc,
            )

    await db.commit()
    # Same reason as ``forward_single_message``: a committed message has expired
    # collections, so they must be reloaded for the response to carry media/reactions.
    edited = await _reload_with_relations(db, int(edited.id))

    await safe_broadcast_many(
        [
            (
                "MESSAGE_EDITED",
                _message_edited_payload(edited, source=EditSource.CRM.value, edit_payload=None),
            )
        ]
    )

    logger.info(
        "operator %s edited message %s in chat %s (edited_in_telegram=%s)",
        operator.username,
        edited.id,
        edited.chat_id,
        edited_in_telegram,
    )
    return MessageUpdateOut(
        message=message_out(edited),
        edited_in_telegram=edited_in_telegram,
    )


# ---------------------------------------------------------------------------
# GET /api/messages/{message_id}/edits — transparency
# ---------------------------------------------------------------------------


@message_router.get(
    "/{message_id}/edits",
    response_model=MessageEditListOut,
    summary="List the Telegram-side edit history of a message",
    responses={404: {"description": "Unknown message"}},
)
async def get_message_edits(
    message_id: int,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageEditListOut:
    """Return every recorded edit — available to **all** operators (§2.3).

    The history deliberately has no permission gate: its whole purpose is that
    anybody on the desk can see what a client originally wrote before editing.
    """
    from app.services.edits import list_edits  # noqa: PLC0415

    message = await _load_message(db, message_id)

    edits = await list_edits(db, int(message.id))
    items: list[MessageEditOut] = []
    for edit in edits:
        payload = _edit_to_payload(edit)
        if not payload:
            continue
        items.append(MessageEditOut.model_validate(payload))

    logger.info(
        "operator %s read %s edit(s) of message %s",
        operator.username,
        len(items),
        message_id,
    )
    return MessageEditListOut(items=items)


# ---------------------------------------------------------------------------
# POST /api/messages/{message_id}/forward — forward as our own message
# ---------------------------------------------------------------------------


@message_router.post(
    "/{message_id}/forward",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Forward a message into another chat as our own message",
    responses={
        404: {"description": "Unknown source message or target chat"},
        409: {"description": "Target chat is locked, or the source was deleted in Telegram"},
        422: {"description": "Target chat equals the source chat"},
    },
)
async def forward_single_message(
    message_id: int,
    payload: ForwardRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageOut:
    """Copy a message into another chat — never a Telegram ``forward_messages``.

    §1.1: the target chat must not learn where the message came from, so
    :func:`app.services.forwarding.forward_message` creates a brand-new outgoing
    row (``forwarded_from`` stays ``NULL``, invariant §5.1) with physical copies of
    the files, and the worker sends them as our own text/media.
    """
    from app.services.forwarding import (  # noqa: PLC0415 - peer module
        forward_message,
        publish_forward_command,
        validate_forward,
    )

    source = await _load_message(db, message_id)
    target = await get_chat(db, int(payload.target_chat_id))

    # §1.2: 422 for the same chat, 409 for a message deleted in Telegram.
    validate_forward(source, target)

    await _acquire_send_guard(int(target.id), operator, db)

    new_message = await forward_message(
        db,
        source_message=source,
        target_chat=target,
        operator=operator,
        comment=payload.comment,
    )

    await db.commit()
    new_message = await _reload_with_relations(db, int(new_message.id))

    attachment_path, attachment_kind = _first_attachment_command_payload(new_message)

    if not _is_mock_chat(target):
        try:
            await publish_forward_command(
                target,
                new_message,
                operator,
                source_chat_id=int(source.chat_id),
                source_message_id=int(source.id),
                attachment_path=attachment_path,
                attachment_kind=attachment_kind,
            )
        except Exception as exc:  # noqa: BLE001 - the stored message survives a Redis outage
            logger.warning(
                "could not publish the forward of message %s into chat %s: %s: %s",
                source.id,
                target.id,
                type(exc).__name__,
                exc,
            )

    await _release_typing_lock(int(target.id), int(operator.id))

    result = message_out(new_message)
    message_payload = result.model_dump(mode="json")
    chat_item = await load_chat_list_item(db, int(target.id))
    chat_payload = chat_item if isinstance(chat_item, dict) else chat_item.model_dump(mode="json")

    await safe_broadcast_many(
        [
            ("NEW_MESSAGE", {**message_payload, "chat": chat_payload}),
            (
                "MESSAGE_FORWARDED",
                {
                    "source_message_id": int(source.id),
                    "source_chat_id": int(source.chat_id),
                    "message": message_payload,
                    "chat": chat_payload,
                },
            ),
        ]
    )

    logger.info(
        "operator %s forwarded message %s (chat %s) into chat %s as message %s (media=%s)",
        operator.username,
        source.id,
        source.chat_id,
        target.id,
        new_message.id,
        attachment_kind,
    )
    return result


# ---------------------------------------------------------------------------
# DELETE /api/messages/{message_id} — delete from CRM, grey trace preserved (§1)
# ---------------------------------------------------------------------------


@message_router.delete(
    "/{message_id}",
    response_model=DeletedMessageOut,
    summary="Удалить сообщение из CRM и продублировать удаление в Telegram",
    responses={
        403: {"description": "У оператора нет права can_delete_messages"},
        404: {"description": "Сообщение не найдено"},
        409: {"description": "Сообщение уже удалено"},
    },
)
async def delete_message_endpoint(
    message_id: int,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletedMessageOut:
    """Soft-delete a message (CONTRACTS_V5 §1.3).

    The row is **never** removed from ``group_messages`` (§1.2): it is flagged
    ``is_deleted_in_tg = True`` with ``deleted_at``/``deleted_source = 'CRM'``, so the
    bubble stays in the timeline as a grey trace. The Telegram deletion is a mirror:
    it is queued **after** the CRM commit, and a delivery failure is reported in
    ``telegram.delivered``/``telegram.error`` without failing the request.

    The permission gate reuses :func:`_operator_outranks` (the §2.3 helper: explicit
    ``can_delete_messages`` or a privileged role). The service repeats the check as
    defence in depth, so no caller can bypass it.

    Domain errors map to HTTP as: :class:`MessageNotFoundError` -> ``404``,
    :class:`MessageAlreadyDeletedError` -> ``409``,
    :class:`MessageDeleteForbiddenError` -> ``403``.
    """
    from app.services.message_delete import (  # noqa: PLC0415 - peer module
        MessageAlreadyDeletedError,
        MessageDeleteError,
        MessageDeleteForbiddenError,
        MessageNotFoundError,
        delete_message,
    )

    if not _operator_outranks(operator):
        logger.warning(
            "operator %s (id=%s) may not delete message %s: can_delete_messages is False",
            operator.username,
            operator.id,
            message_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: удаление сообщений запрещено",
        )

    try:
        return await delete_message(db, message_id=int(message_id), operator=operator)
    except MessageNotFoundError as exc:
        logger.warning("delete of unknown message %s", message_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Сообщение #{int(message_id)} не найдено",
        ) from exc
    except MessageAlreadyDeletedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except MessageDeleteForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except MessageDeleteError as exc:
        logger.exception("could not delete message %s", message_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ``router`` (``/chats``) and ``message_router`` (``/messages``) are mounted SEPARATELY
# by ``main.mount_router``: it mounts ``router`` under ``/api`` and then discovers any
# ``<name>_router`` attribute and mounts it under ``/api`` as well. Nesting
# ``message_router`` into ``router`` here would prefix it with ``/chats``, yielding
# ``/api/chats/messages/...`` instead of the documented ``/api/messages/...``
# (CONTRACTS_V3 §2.3) — so no ``include_router`` call belongs in this module.

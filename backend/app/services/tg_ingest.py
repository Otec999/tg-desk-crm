"""Bridge from the Redis/Telethon channel into PostgreSQL + WebSocket fan-out.

This module owns the worker -> backend direction of CONTRACTS.md §5:

===============================  ===========================================================
channel                          handler
===============================  ===========================================================
``tg_incoming``                  :func:`handle_incoming`
``tg_deleted``                   :func:`handle_deleted`
``tg_edits``                     :func:`handle_edit`
``tg_outgoing_ack``              :func:`handle_outgoing_ack`
===============================  ===========================================================

Design rules
------------
* **Never raise.** ``background.redis_consumer`` logs and drops a payload that raises, but a
  single malformed frame must never cost the bus its subscription — every handler is defensive
  and degrades to a warning.
* **Never delete rows.** Telegram deletions are recorded as ``is_deleted_in_tg`` / ``deleted_at``
  so the CRM keeps its audit trail (CONTRACTS.md §3.3 ``MessageOut``).
* **Idempotent.** Telegram redelivers frames after a worker restart; messages are de-duplicated on
  ``(chat_id, tg_msg_id)`` and acks are de-duplicated by their resulting state.
* **Own the transaction.** Each handler creates its session from ``session_factory`` and commits
  once, then broadcasts. Broadcasts happen *after* the commit so no client can observe a state
  the database does not have yet.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeliveryStatus,
    GroupChat,
    GroupMessage,
    MediaAttachment,
    MessageKind,
    MessageSenderType,
    Operator,
    SLAIncident,
)
from app.schemas import (
    ChatListItem,
    MessageOut,
    WsChatAssignedData,
    WsMessageDeletedData,
    WsMessageStatusData,
    WsNewMessageData,
    dump_model,
)
from app.services import sla_engine
from app.services.reactions import apply_telegram_reactions
from app.utils import extract_phone, iso_z, sanitize_telegram_text, utcnow
from app.ws_manager import (
    EVT_CHAT_ASSIGNED,
    EVT_MESSAGE_DELETED,
    EVT_MESSAGE_STATUS,
    EVT_NEW_MESSAGE,
    EVT_SLA_UPDATE,
    envelope,
    manager,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EVT_MESSAGE_EDITED",
    "derive_merchant_code",
    "handle_deleted",
    "handle_edit",
    "handle_incoming",
    "handle_outgoing_ack",
]

#: Additive WS event for text edits (CONTRACTS.md §5 ``tg_edits``). The frontend ignores unknown
#: types, so emitting it next to the frozen set is backwards compatible.
EVT_MESSAGE_EDITED: Final[str] = "MESSAGE_EDITED"

#: Broadcaster shape used by ``background.BackgroundContext.broadcast``.
BroadcastFn = Callable[..., Awaitable[Any]]

#: Session factory shape (``app.database.SessionLocal``).
SessionFactory = Callable[[], Any]

_TITLE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+")
_CODE_SANITIZER_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9]+")


# ============================================================================ small helpers
def _enum_value(value: Any) -> str:
    """Plain string of an enum-typed value (``DeliveryStatus.SENT`` -> ``"sent"``)."""
    return str(getattr(value, "value", value) or "")


def _coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion for ids coming from JSON (``None`` on failure)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any, *, limit: int | None = None) -> str | None:
    """Best-effort trimmed string coercion (``None`` for empty input)."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _parse_tg_datetime(value: Any) -> datetime | None:
    """Parse a Telegram ISO timestamp into an aware UTC datetime (``None`` on failure)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.debug("Unparseable tg_date %r, falling back to now()", value)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def derive_merchant_code(title: str | None, fallback_chat_id: int | None = None) -> str | None:
    """Derive ``MRC-<FIRSTTOKEN>`` from a chat title (CONTRACTS.md §5 ``merchant_code``).

    ``"Merchant Alpha — Payouts"`` -> ``"MRC-ALPHA"``. Titles whose leading token is not
    alphanumeric (or that are empty) fall back to the Telegram chat id, so the column is never
    ``NULL`` for a chat created by the worker.
    """
    if title:
        for candidate in _TITLE_TOKEN_RE.findall(title):
            token = _CODE_SANITIZER_RE.sub("", candidate.upper())
            # Skip the generic "Merchant" prefix: the meaningful token is the one after it.
            if token and token != "MERCHANT":
                return f"MRC-{token[:24]}"
            if token == "MERCHANT":
                continue
    if fallback_chat_id is not None:
        digits = str(abs(int(fallback_chat_id)))
        return f"MRC-{digits[-6:] or 'UNKNOWN'}"
    return None


# ============================================================================ serialisation
def _chat_list_item(
    chat: GroupChat,
    last_message: GroupMessage | None,
    incident: SLAIncident | None,
) -> ChatListItem:
    """Build the ``ChatListItem`` object for ``NEW_MESSAGE.data.chat`` (CONTRACTS.md §3.3)."""
    return ChatListItem.build(
        chat,
        last_message=last_message,
        sla=incident if incident is not None and _enum_value(incident.status) == "PENDING" else None,
        active_sla_count=1 if incident is not None else 0,
    )


# ============================================================================ chat resolution
async def _get_or_create_chat(db: AsyncSession, payload: dict[str, Any]) -> GroupChat | None:
    """Find the ``GroupChat`` for ``payload['tg_chat_id']``, creating it when unknown.

    The chat is created from ``chat_title`` / ``chat_username`` / ``merchant_code``; when the
    worker does not send a merchant code it is derived from the title (``MRC-<TOKEN>``). Returns
    ``None`` when the payload carries no usable ``tg_chat_id``.
    """
    tg_chat_id = _coerce_int(payload.get("tg_chat_id"))
    if tg_chat_id is None:
        logger.warning("tg_ingest: payload without tg_chat_id (%s), ignored", payload.get("kind"))
        return None

    existing = (
        await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == tg_chat_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    title = _coerce_str(payload.get("chat_title"), limit=255) or f"Chat {tg_chat_id}"
    username = _coerce_str(payload.get("chat_username"), limit=64)
    merchant_code = _coerce_str(payload.get("merchant_code"), limit=64) or derive_merchant_code(
        title, tg_chat_id
    )
    chat = GroupChat(
        tg_chat_id=tg_chat_id,
        title=title,
        username=username,
        merchant_code=merchant_code,
        unread_count=0,
    )
    db.add(chat)
    await db.flush()
    logger.info(
        "tg_ingest: created chat %s for tg_chat_id=%s (%s / %s)",
        chat.id,
        tg_chat_id,
        title,
        merchant_code,
    )
    return chat


async def _resolve_reply(
    db: AsyncSession,
    chat_id: int,
    reply_to_tg_msg_id: int | None,
) -> tuple[int | None, int | None]:
    """Resolve a quoted Telegram message id to ``(crm_id, tg_id)``.

    Returns ``(None, reply_to_tg_msg_id)`` when the quoted message is not stored: the ``tg`` id
    is still persisted so a late-arriving history backfill can be linked later.
    """
    tg_id = _coerce_int(reply_to_tg_msg_id)
    if tg_id is None:
        return None, None
    found = (
        await db.execute(
            select(GroupMessage.id)
            .where(GroupMessage.chat_id == chat_id, GroupMessage.tg_msg_id == tg_id)
            .order_by(GroupMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (_coerce_int(found), tg_id)


async def _find_message_by_tg_id(
    db: AsyncSession,
    chat_id: int,
    tg_msg_id: int,
) -> GroupMessage | None:
    """Newest message of ``chat_id`` carrying ``tg_msg_id`` (``None`` when absent)."""
    stmt = (
        select(GroupMessage)
        .where(GroupMessage.chat_id == chat_id, GroupMessage.tg_msg_id == tg_msg_id)
        .order_by(GroupMessage.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ============================================================================ inbound message
async def handle_incoming(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any],
) -> None:
    """Persist one message received on ``tg_incoming`` and fan it out over WebSocket.

    Steps:

    1. resolve (or create) the chat by ``tg_chat_id``;
    2. de-duplicate on ``(chat_id, tg_msg_id)`` — Telegram redelivers after a restart;
    3. store the message (``sender_type`` = ``BOT`` when ``is_bot``, else ``CLIENT``), sanitised
       text, extracted phone, and the resolved reply link;
    4. bump ``unread_count`` and ``last_message_at``;
    5. open (or reuse) the SLA clock via :func:`sla_engine.open_incident`;
    6. commit, then broadcast ``NEW_MESSAGE`` and — when this message opened a clock —
       ``SLA_UPDATE`` plus ``CHAT_ASSIGNED``.

    Never raises: a broken payload is logged and ignored.
    """
    if not isinstance(payload, dict):
        logger.warning("handle_incoming: non-dict payload (%s), ignored", type(payload).__name__)
        return

    try:
        async with session_factory() as db:
            chat = await _get_or_create_chat(db, payload)
            if chat is None:
                return

            tg_msg_id = _coerce_int(payload.get("tg_msg_id"))
            if tg_msg_id is not None:
                duplicate = await _find_message_by_tg_id(db, chat.id, tg_msg_id)
                if duplicate is not None:
                    logger.info(
                        "handle_incoming: duplicate tg_msg_id=%s in chat %s, ignored",
                        tg_msg_id,
                        chat.id,
                    )
                    return

            raw_text = payload.get("text")
            text = sanitize_telegram_text(raw_text if isinstance(raw_text, str) else "")
            reply_to_id, reply_to_tg = await _resolve_reply(
                db, chat.id, _coerce_int(payload.get("reply_to_tg_msg_id"))
            )
            sender_name = _coerce_str(payload.get("sender_name"), limit=255)
            sender_username = _coerce_str(payload.get("sender_username"), limit=128)
            if sender_name is None:
                sender_name = sender_username or "Unknown"

            message = GroupMessage(
                chat_id=chat.id,
                tg_msg_id=tg_msg_id,
                sender_type=(
                    MessageSenderType.BOT.value
                    if bool(payload.get("is_bot")) or payload.get("sender_type") == "BOT"
                    else MessageSenderType.CLIENT.value
                ),
                sender_operator_id=None,
                sender_name=sender_name,
                sender_tg_id=_coerce_int(payload.get("sender_tg_id")),
                sender_username=sender_username,
                text=text,
                reply_to_id=reply_to_id,
                reply_to_tg_msg_id=reply_to_tg,
                phone=extract_phone(text),
                is_deleted_in_tg=False,
                deleted_at=None,
                delivery_status=DeliveryStatus.SENT.value,
                client_nonce=None,
            )

            # CONTRACTS_V2 §2.4/§4: media type and caption travel alongside the text.
            caption = _coerce_str(payload.get("caption"), limit=1024)
            raw_media = payload.get("media")
            media_records = raw_media if isinstance(raw_media, list) else []
            message_kind = _coerce_str(payload.get("message_kind"), limit=16)
            if caption:
                message.caption = caption
            if message_kind:
                message.kind = message_kind
            elif media_records:
                message.kind = MessageKind.PHOTO.value

            created_at = _parse_tg_datetime(payload.get("tg_date"))
            if created_at is not None:
                message.created_at = created_at
                message.updated_at = created_at

            db.add(message)
            await db.flush()

            # CONTRACTS_V2 §3.1: one MediaAttachment row per downloaded file. The
            # worker stores files under {media_root}/chats/{tg_chat_id}/{tg_msg_id}/…
            # and reports POSIX-relative paths, which map 1:1 onto media_root.
            attachments: list[MediaAttachment] = []
            for record in media_records:
                attachment = _build_attachment(record, message_id=int(message.id))
                if attachment is not None:
                    db.add(attachment)
                    attachments.append(attachment)
            if attachments:
                if not message.kind or message.kind == MessageKind.TEXT.value:
                    message.kind = getattr(attachments[0], "kind", None) or MessageKind.DOCUMENT.value
                await db.flush()

            chat.unread_count = int(chat.unread_count or 0) + 1
            chat.last_message_at = message.created_at or utcnow()

            had_pending = await sla_engine.has_pending_incident(db, chat.id)
            incident = await sla_engine.open_incident(db, message)
            opened_clock = incident is not None and not had_pending

            await db.commit()

            chat_item = _chat_list_item(chat, message, incident if opened_clock else None)
            message_on_wire = MessageOut.from_orm_message(message)
            operator_id = getattr(incident, "assigned_operator_id", None) if incident is not None else None
            operator_name = (
                await _operator_display_name(db, operator_id) if operator_id is not None else None
            )

        await _safe_broadcast(
            broadcast,
            EVT_NEW_MESSAGE,
            WsNewMessageData(message=message_on_wire, chat=chat_item).to_envelope_data(),
        )

        if opened_clock and incident is not None:
            await _safe_broadcast(
                broadcast,
                EVT_SLA_UPDATE,
                sla_engine.sla_update_payload(
                    incident,
                    chat=chat,
                    assigned_operator_name=operator_name,
                ),
            )
            await _safe_broadcast(
                broadcast,
                EVT_CHAT_ASSIGNED,
                dump_model(
                    WsChatAssignedData(
                        chat_id=chat.id,
                        assigned_operator_id=operator_id,
                        assigned_operator_name=operator_name,
                    )
                ),
            )
    except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the Redis consumer
        logger.exception("handle_incoming failed (%s: %s)", type(exc).__name__, exc)


#: Media kinds the worker may report; anything else degrades to ``OTHER``.
_VALID_MEDIA_KINDS = frozenset(kind.value for kind in MessageKind)


def _build_attachment(record: Any, *, message_id: int) -> MediaAttachment | None:
    """Map one worker ``media[]`` record onto a :class:`MediaAttachment` row.

    The worker's contract (CONTRACTS_V2 §5) is a dict with ``kind, file_name,
    mime_type, size_bytes, width, height, duration_seconds, storage_path,
    tg_file_id, thumbnail_path``. Records without ``storage_path`` describe an
    attachment that was too large to download: they are still stored so the UI can
    show "файл слишком большой" instead of silently dropping the message.

    Returns ``None`` for records that are not dicts or carry no usable identity.
    """
    if not isinstance(record, dict):
        return None

    storage_path = record.get("storage_path")
    thumbnail_path = record.get("thumbnail_path")

    def _clean_path(value: Any) -> str | None:
        """Keep only POSIX-relative paths — an absolute path would escape media_root."""
        if not isinstance(value, str):
            return None
        candidate = value.strip().replace("\\", "/")
        if not candidate or candidate.startswith("/") or ":" in candidate or ".." in candidate.split("/"):
            logger.warning("shared: rejecting suspicious media path %r", value)
            return None
        return candidate

    safe_storage = _clean_path(storage_path)
    safe_thumb = _clean_path(thumbnail_path)
    if safe_storage is None and safe_thumb is None:
        # Nothing downloadable and no preview — the worker still wants the message kind
        # recorded, but an empty attachment row would render as a broken file chip.
        return None

    kind = _coerce_str(record.get("kind"), limit=16)
    if kind is None or kind not in _VALID_MEDIA_KINDS:
        kind = MessageKind.OTHER.value

    try:
        attachment = MediaAttachment(
            message_id=int(message_id),
            kind=kind,
            file_name=_coerce_str(record.get("file_name"), limit=255),
            mime_type=_coerce_str(record.get("mime_type"), limit=128),
            size_bytes=_coerce_int(record.get("size_bytes")),
            width=_coerce_int(record.get("width")),
            height=_coerce_int(record.get("height")),
            duration_seconds=_coerce_int(record.get("duration_seconds")),
            storage_path=safe_storage,
            thumbnail_path=safe_thumb,
            tg_file_id=_coerce_str(record.get("tg_file_id"), limit=255),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("shared: could not build an attachment from %r (%s)", record, exc)
        return None
    return attachment


async def _operator_display_name(db: AsyncSession, operator_id: int | None) -> str | None:
    """Display name of ``operator_id`` (``None`` when unassigned/unknown), never raises."""
    if operator_id is None:
        return None
    try:
        return (
            await db.execute(select(Operator.display_name).where(Operator.id == int(operator_id)))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - cosmetic lookup
        return None


# ============================================================================ deletions
async def handle_deleted(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any],
) -> None:
    """Record Telegram-side deletions as a soft flag — the rows are **never** deleted.

    CONTRACTS.md §5 payload::

        {"kind":"MESSAGE_DELETED","tg_chat_id":-1001234567890,"tg_msg_ids":[90210,90211]}

    Telegram also delivers this frame with an **empty** ``tg_msg_ids`` list when a whole
    conversation disappears on the Telegram side; that case is a safe no-op (the CRM keeps
    everything it knows).

    Broadcast per message: ``MESSAGE_DELETED`` ``{message_id, chat_id, deleted_at, message}``.
    """
    if not isinstance(payload, dict):
        logger.warning("handle_deleted: non-dict payload (%s), ignored", type(payload).__name__)
        return

    try:
        raw_ids = payload.get("tg_msg_ids")
        candidates: Iterable[Any] = raw_ids if isinstance(raw_ids, (list, tuple)) else []
        tg_msg_ids = [value for value in (_coerce_int(item) for item in candidates) if value is not None]
        if not tg_msg_ids:
            logger.info(
                "handle_deleted: empty tg_msg_ids for tg_chat_id=%s (full delete), nothing to flag",
                payload.get("tg_chat_id"),
            )
            return

        tg_chat_id = _coerce_int(payload.get("tg_chat_id"))
        if tg_chat_id is None:
            logger.warning("handle_deleted: payload without tg_chat_id, ignored")
            return

        async with session_factory() as db:
            chat = (
                await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == tg_chat_id))
            ).scalar_one_or_none()
            if chat is None:
                logger.info("handle_deleted: unknown chat tg_chat_id=%s, ignored", tg_chat_id)
                return

            now = utcnow()
            changed: list[GroupMessage] = []
            for tg_msg_id in tg_msg_ids:
                message = await _find_message_by_tg_id(db, chat.id, tg_msg_id)
                if message is None:
                    logger.debug(
                        "handle_deleted: tg_msg_id=%s not stored in chat %s", tg_msg_id, chat.id
                    )
                    continue
                if bool(message.is_deleted_in_tg) and message.deleted_at is not None:
                    continue
                message.is_deleted_in_tg = True
                message.deleted_at = now
                changed.append(message)

            if not changed:
                return
            await db.commit()

            events = [
                WsMessageDeletedData.from_orm_message(message) for message in changed
            ]

        for event in events:
            await _safe_broadcast(broadcast, EVT_MESSAGE_DELETED, dump_model(event))
        logger.info(
            "handle_deleted: flagged %d message(s) as deleted in chat %s", len(events), chat.id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("handle_deleted failed (%s: %s)", type(exc).__name__, exc)


# ============================================================================ edits
async def handle_edit(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any] | Any,
) -> None:
    """Apply a Telegram-side text edit and broadcast ``MESSAGE_EDITED``.

    CONTRACTS_V3 §2.5 payload (``tg_edits`` channel)::

        {"kind":"MESSAGE_EDITED","tg_chat_id":-100...,"tg_msg_id":90210,"text":"...",
         "edited_at":"...","actor_name":"Jane Doe","actor_username":"jane"}

    ``actor_name``/``actor_username``/``edited_at`` are **optional**: a worker
    that still sends the v1 ``{tg_chat_id, tg_msg_id, text}`` frame keeps working,
    the missing display name is simply left ``NULL`` in the history.

    The text change itself is delegated to
    :func:`app.services.edits.apply_telegram_edit`, which is the only place that
    knows the asymmetry of CONTRACTS_V3 §2.1: a Telegram edit **preserves the
    previous version** in ``message_edits``. The chat is still resolved by
    ``tg_chat_id`` first (the worker reports a Telegram id, not a CRM one).

    Broadcast ``MESSAGE_EDITED`` (§2.6)::

        {"message_id", "chat_id", "text", "edited_at",
         "source": "TELEGRAM", "edit": <MessageEditOut | null>}

    ``edit`` is ``null`` when nothing was recorded — an unknown/deleted message or
    a frame whose text matches what the CRM already stores (idempotent
    redelivery). Such a frame is a **safe no-op**: no event is emitted, so two
    identical edits cannot make the UI render an empty history.

    Never raises: a malformed frame is logged and dropped so the Redis consumer
    keeps its subscription.
    """
    if not isinstance(payload, dict):
        logger.warning("handle_edit: non-dict payload (%s), ignored", type(payload).__name__)
        return

    try:
        tg_chat_id = _coerce_int(payload.get("tg_chat_id"))
        tg_msg_id = _coerce_int(payload.get("tg_msg_id"))
        if tg_chat_id is None or tg_msg_id is None:
            logger.warning("handle_edit: payload missing tg_chat_id/tg_msg_id, ignored")
            return

        # ``actor_name`` is the contract's fallback when Telethon cannot resolve
        # the sender entity; ``actor_username`` is used when only that survived.
        actor_name = _coerce_str(payload.get("actor_name"), limit=255) or _coerce_str(
            payload.get("actor_username"), limit=255
        )
        edited_at = _parse_tg_datetime(payload.get("edited_at"))
        raw_text = payload.get("text")
        new_text = raw_text if isinstance(raw_text, str) else ""

        async with session_factory() as db:
            chat = (
                await db.execute(select(GroupChat).where(GroupChat.tg_chat_id == tg_chat_id))
            ).scalar_one_or_none()
            if chat is None:
                logger.info("handle_edit: unknown chat tg_chat_id=%s, ignored", tg_chat_id)
                return

            # Local import keeps the two services loosely coupled and pinpoints the
            # only place ``tg_ingest`` knows about edit history.
            from app.services.edits import apply_telegram_edit, edit_to_dict

            result = await apply_telegram_edit(
                db,
                chat_id=int(chat.id),
                tg_msg_id=tg_msg_id,
                new_text=new_text,
                actor_name=actor_name,
                edited_at=edited_at,
            )

            if result is None:
                # Unknown message or a message already deleted in Telegram:
                # ``apply_telegram_edit`` logged the reason. Roll back instead of
                # committing, so the ``FOR UPDATE`` lock is released immediately.
                await db.rollback()
                return

            message, edit = result
            # Snapshot every attribute we still need **before** any rollback:
            # ``AsyncSession.rollback()`` expires all loaded instances, and reading
            # an expired column without a greenlet context raises ``MissingGreenlet``.
            message_id = int(message.id)
            message_chat_id = int(message.chat_id)
            message_text = message.text or ""
            edited_at_text = iso_z(message.updated_at)

            if edit is None:
                # Idempotent redelivery: the row already holds this text, so there
                # is nothing new to show — and no history row was written.
                await db.rollback()
                logger.info(
                    "handle_edit: message %s already carries the edited text, no event emitted",
                    message_id,
                )
                return

            await db.commit()

            event: dict[str, Any] = {
                "message_id": message_id,
                "chat_id": message_chat_id,
                "text": message_text,
                "edited_at": edited_at_text,
                "source": "TELEGRAM",
                "edit": edit_to_dict(edit),
            }

        await _safe_broadcast(broadcast, EVT_MESSAGE_EDITED, event)
        logger.info(
            "handle_edit: message %s in chat %s updated (history row %s)",
            event["message_id"],
            event["chat_id"],
            (event["edit"] or {}).get("id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("handle_edit failed (%s: %s)", type(exc).__name__, exc)


# ============================================================================ outgoing acks
async def handle_outgoing_ack(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any],
) -> None:
    """Apply the Telethon worker's delivery acknowledgement.

    CONTRACTS.md §5 payload::

        {"cmd_id":"uuid","chat_id":1,"message_id":55,"tg_msg_id":90210,"ok":true,"error":null}

    ``ok=True`` -> ``delivery_status='sent'`` and the Telegram id is stored (so later quotes of
    this message resolve). ``ok=False`` -> ``delivery_status='failed'``; the text is left
    untouched. Broadcast: ``MESSAGE_STATUS`` ``{message_id, chat_id, delivery_status, tg_msg_id}``.
    """
    if not isinstance(payload, dict):
        logger.warning("handle_outgoing_ack: non-dict payload (%s), ignored", type(payload).__name__)
        return

    try:
        message_id = _coerce_int(payload.get("message_id"))
        if message_id is None:
            logger.warning("handle_outgoing_ack: payload without message_id, ignored")
            return

        ok = bool(payload.get("ok"))
        tg_msg_id = _coerce_int(payload.get("tg_msg_id"))

        async with session_factory() as db:
            message = (
                await db.execute(select(GroupMessage).where(GroupMessage.id == message_id))
            ).scalar_one_or_none()
            if message is None:
                logger.warning("handle_outgoing_ack: message %s no longer exists", message_id)
                return

            if ok:
                message.delivery_status = DeliveryStatus.SENT.value
                if tg_msg_id is not None:
                    message.tg_msg_id = tg_msg_id
            else:
                message.delivery_status = DeliveryStatus.FAILED.value
                logger.warning(
                    "handle_outgoing_ack: delivery of message %s failed: %s",
                    message_id,
                    payload.get("error"),
                )

            await db.commit()

            event = dump_model(
                WsMessageStatusData(
                    message_id=int(message.id),
                    chat_id=int(message.chat_id),
                    delivery_status=_enum_value(message.delivery_status),
                    tg_msg_id=message.tg_msg_id,
                )
            )

        await _safe_broadcast(broadcast, EVT_MESSAGE_STATUS, event)
    except Exception as exc:  # noqa: BLE001
        logger.exception("handle_outgoing_ack failed (%s: %s)", type(exc).__name__, exc)


async def handle_reactions(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any],
) -> None:
    """Apply a reaction change received from the Telethon worker.

    CONTRACTS_V2 §4 ``tg_reactions`` payload::

        {"kind":"REACTION_UPDATE","tg_chat_id":-100123,"tg_msg_id":90210,
         "reactions":[{"emoji":"👍","count":1,"user_ids":[555]}]}

    The worker sends the *complete* reaction set for the message, so
    :func:`app.services.reactions.apply_telegram_reactions` replaces every
    ``source=TELEGRAM`` row while leaving operator (``source=CRM``) rows intact.
    That service commits and broadcasts its own ``MESSAGE_REACTION*`` events and
    swallows its own errors, so this handler only owns the chat lookup and the
    frame-level error log — a second ``commit()`` here would be redundant.

    A message that is not stored yet is **not** applied: the frame is dropped with a
    warning instead of being mirrored onto the wrong row (Telegram delivers a
    reaction only after its message, so a missing row means the worker's
    ``tg_incoming`` frame has not been consumed yet).
    """
    if not isinstance(payload, dict):
        logger.warning("handle_reactions: non-dict payload (%s), ignored", type(payload).__name__)
        return

    tg_chat_id = _coerce_int(payload.get("tg_chat_id"))
    tg_msg_id = _coerce_int(payload.get("tg_msg_id"))
    chat_id = _coerce_int(payload.get("chat_id"))
    raw_reactions = payload.get("reactions")

    if tg_msg_id is None or not isinstance(raw_reactions, list):
        logger.warning("handle_reactions: payload without tg_msg_id/reactions, ignored")
        return

    try:
        async with session_factory() as db:
            # ``chat_id`` is the CRM id when present; otherwise resolve it from the
            # Telegram id so a worker that only knows tg_chat_id still works.
            if chat_id is None and tg_chat_id is not None:
                chat_id = _coerce_int(
                    (
                        await db.execute(
                            select(GroupChat.id).where(GroupChat.tg_chat_id == tg_chat_id).limit(1)
                        )
                    ).scalar_one_or_none()
                )

            if chat_id is None:
                logger.info(
                    "handle_reactions: chat for tg_chat_id=%s is unknown, ignored", tg_chat_id
                )
                return

            if await _find_message_by_tg_id(db, int(chat_id), int(tg_msg_id)) is None:
                logger.info(
                    "handle_reactions: tg_msg_id=%s is not stored in chat %s yet, ignored",
                    tg_msg_id,
                    chat_id,
                )
                await db.rollback()
                return

            await apply_telegram_reactions(
                db, chat_id=int(chat_id), tg_msg_id=int(tg_msg_id), reactions=raw_reactions
            )

        logger.info(
            "handle_reactions: synced %s emoji for chat %s / tg_msg_id %s",
            len(raw_reactions),
            chat_id,
            tg_msg_id,
        )
    except Exception as exc:  # noqa: BLE001 - a bad frame must never kill the consumer
        logger.exception("handle_reactions failed (%s: %s)", type(exc).__name__, exc)


# ============================================================================ broadcast plumbing
async def _safe_broadcast(
    broadcast: BroadcastFn | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Send one envelope through ``broadcast`` (``manager.broadcast`` by default).

    The production callable is ``manager.broadcast(envelope)``; a callable exposed as
    ``broadcast(type, data)`` is tolerated via the ``TypeError`` fallback. Errors are logged and
    never raised, so a dead WebSocket client can never fail a Redis frame.
    """
    target = broadcast if broadcast is not None else manager.broadcast
    try:
        await target(envelope(event_type, data))
        return
    except TypeError:
        pass
    except Exception as exc:  # noqa: BLE001 - fan-out is best-effort
        logger.warning("broadcast of %s failed: %s: %s", event_type, type(exc).__name__, exc)
        return
    try:
        await target(event_type, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("broadcast of %s failed: %s: %s", event_type, type(exc).__name__, exc)

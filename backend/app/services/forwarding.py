"""Cross-chat message forwarding for the desk (CONTRACTS_V3.md §1).

An operator takes a message from chat A (photo + caption, or text only) and sends
it into chat B **as their own message**: chat B must never learn that the message
came from another chat or from another person.

Why this module exists instead of Telegram's own forward
-------------------------------------------------------
``client.forward_messages`` renders a "Forwarded from …" header, which leaks the
original author (invariant §5.1) and cannot be suppressed. The contract therefore
mandates a *recreation*:

1. the text (``comment`` + caption/текст) becomes **our own** text;
2. every attachment is **physically copied** on disk into the target message's
   folder and re-inserted as a brand new :class:`~app.models.MediaAttachment`
   row (invariant §5.6 — two messages never share one file), so deleting the
   source later cannot break the copy;
3. ``forwarded_from`` is always ``NULL`` (invariant §5.1);
4. the worker is told to ``send_file`` / ``send_message`` on the new row — never
   to forward — through the dedicated :data:`CH_TG_FORWARD` Redis channel (§1.3).

Design rules
------------
* **Invariants are not configurable.** ``forwarded_from`` is written as ``None``
  literally, in one place, and asserted by tests.
* **Media is fail-soft, the message is not.** A copy that fails (source file
  gone, disk full, permission denied) is logged and skipped: a forwarded message
  without its file is still better than no message at all, and a half-copied
  album must not roll back the text.
* **Only relative POSIX paths in the database.** ``storage_path`` /
  ``thumbnail_path`` are always ``chats/{chat_id}/{message_id}/{name}``; the
  absolute path is computed from :func:`~app.services.media.media_root` on every
  access, so the media tree stays relocatable.
* **No path escapes.** Every source path goes through
  :func:`~app.services.media.resolve_media_path` and every destination path is
  built from integers, so a poisoned row cannot make us copy ``C:\\Windows\\…``
  into the media tree.
* **The caller owns the transaction.** :func:`forward_message` issues ``flush()``
  only (so the new row id is available to the REST layer and to the Redis
  payload) and leaves the ``commit()`` to the router — exactly like
  :func:`app.services.sla_engine.resolve_for_reply`.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy.orm import object_session
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DeliveryStatus, MessageKind, MessageSenderType
from app.models import GroupChat, GroupMessage, MediaAttachment, Operator
from app.services import sla_engine
from app.services.media import (
    MEDIA_ROOT_SUBDIR,
    THUMBNAIL_SUFFIX,
    media_root,
    resolve_media_path,
    safe_file_name,
)
from app.utils import extract_phone, sanitize_telegram_text, utcnow

logger = logging.getLogger("tgdesk.forwarding")

__all__ = [
    "CH_TG_FORWARD",
    "MAX_CAPTION_LENGTH",
    "MAX_TEXT_LENGTH",
    "build_forward_text",
    "copy_attachment",
    "forward_message",
    "message_label",
    "publish_forward_command",
    "validate_forward",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Redis channel consumed by the Telethon worker for recreated sends (§1.3).
#: Declared locally on purpose: this module owns the semantic, ``app.redis_bus``
#: is owned by another agent and will re-export it from here.
CH_TG_FORWARD: Final[str] = "tg_forward"

#: Hard cap enforced by Telegram and by the ``ck_group_messages_text_length_valid``
#: CHECK constraint — mirrors :data:`app.routers.messages.MAX_TEXT_LENGTH`.
#: Kept as a literal instead of importing the router: the dependency direction is
#: router -> service, never the reverse.
MAX_TEXT_LENGTH: Final[int] = 4096

#: ``ck_group_messages_caption_length_valid`` — Telegram's own caption limit.
MAX_CAPTION_LENGTH: Final[int] = 1024

#: Fallback when an attachment carries no usable name.
_FALLBACK_FILE_NAME: Final[str] = "file"

#: How many collision suffixes (``photo~2.jpg`` …) we try before falling back to a
#: uuid-prefixed name. Sized for an album of duplicates, not a directory scan.
_MAX_DEDUP_ATTEMPTS: Final[int] = 1000

#: Message kinds that always denote media (the caption is shown instead of text).
_NON_TEXT_KINDS: Final[frozenset[str]] = frozenset(
    kind.value for kind in MessageKind if kind is not MessageKind.TEXT
)


# ---------------------------------------------------------------------------
# Text assembly
# ---------------------------------------------------------------------------


def _normalise_lines(text: str) -> str:
    """Trim trailing spaces per line, normalise CRLF, drop the outer blank lines."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _source_body(message: GroupMessage) -> str:
    """Text of the source message: the caption for media, else the text.

    Telegram keeps a media caption apart from the message text, so a photo with a
    caption has ``caption="Смотри"`` and ``text=""``. The caption wins; when an
    ingest path also mirrored it into ``text`` the duplicate is collapsed instead
    of being pasted twice.
    """
    caption = _normalise_lines(str(getattr(message, "caption", None) or ""))
    body = _normalise_lines(str(getattr(message, "text", None) or ""))
    if caption and body and caption != body:
        return f"{caption}\n\n{body}"
    return caption or body


def build_forward_text(source_message: GroupMessage, comment: str | None = None) -> str:
    """Contract §1.4 text: ``comment``, a blank line, then the source body.

    The comment goes **on top** so the operator's note reads first and the
    forwarded content follows. The result is sanitised
    (:func:`~app.utils.sanitize_telegram_text`, which already collapses 3+ blank
    lines) and capped at :data:`MAX_TEXT_LENGTH`.

    An empty result is legitimate: a media message without a caption and without a
    comment forwards as "media only", exactly like the original.
    """
    head = _normalise_lines(str(comment or ""))
    body = _source_body(source_message)
    combined = f"{head}\n\n{body}" if head and body else (head or body)
    text = sanitize_telegram_text(combined)
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH].rstrip()
    return text


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def validate_forward(source_message: GroupMessage, target_chat: GroupChat) -> None:
    """Refuse a forward that must not happen (§1.2).

    * ``422`` — the target chat **is** the source chat: forwarding into the same
      conversation would duplicate the message and point the new copy back at
      the original author, so the contract forbids it outright;
    * ``409`` — the source message was deleted in Telegram: we would be telling
      our account to send content that no longer exists.

    Both checks read the objects the caller already loaded (no extra query); the
    order matters because ``422`` is the more specific verdict.
    """
    source_chat_id = getattr(source_message, "chat_id", None)
    target_chat_id = getattr(target_chat, "id", None)
    if (
        source_chat_id is not None
        and target_chat_id is not None
        and int(source_chat_id) == int(target_chat_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нельзя переслать сообщение в тот же чат",
        )
    if bool(getattr(source_message, "is_deleted_in_tg", False)) or getattr(
        source_message, "deleted_at", None
    ) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сообщение удалено в Telegram — пересылка невозможна",
        )


# ---------------------------------------------------------------------------
# File copying
# ---------------------------------------------------------------------------


def _unique_name(directory: Path, file_name: str) -> str:
    """Pick an unused name in ``directory`` derived from ``file_name``.

    The original name is preferred so the worker uploads a recognisable file to
    Telegram (``invoice.pdf`` instead of ``9f1c….pdf``). On a collision it tries
    ``stem~2.ext``, ``stem~3.ext`` …, and finally a uuid-prefixed name that cannot
    collide in practice. The folder is owned by exactly one message, so the scan
    is bounded by that message's attachment count.
    """
    wanted = safe_file_name(file_name, fallback=_FALLBACK_FILE_NAME)
    stem = Path(wanted).stem or _FALLBACK_FILE_NAME
    suffix = Path(wanted).suffix
    for index in range(1, _MAX_DEDUP_ATTEMPTS):
        candidate = wanted if index == 1 else f"{stem}~{index}{suffix}"
        if not (directory / candidate).exists():
            return candidate
    return f"{uuid.uuid4().hex}-{wanted}"


def _same_file(left: Path, right: Path) -> bool:
    """``True`` when both paths really point at the same file on disk."""
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:  # pragma: no cover - exotic filesystem race
        return False


def _copy_file(source: Path, destination: Path) -> bool:
    """``shutil.copy2`` that logs instead of raising; ``True`` on a usable copy.

    A copy onto itself (the source already sits in the destination folder) is a
    no-op success, which keeps a re-entrant call from truncating the original.
    A failed copy removes its own partial file so no orphan is left behind.
    """
    try:
        if _same_file(source, destination):
            return True
        shutil.copy2(source, destination)
        return destination.is_file() and destination.stat().st_size > 0
    except FileNotFoundError:
        logger.warning("файл-источник исчез, копирование пропущено: %s", source)
    except OSError as exc:
        logger.warning(
            "не удалось скопировать %s -> %s: %s: %s",
            source,
            destination,
            type(exc).__name__,
            exc,
        )
        try:
            destination.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - cleanup is cosmetic
            pass
    return False


def _relative_storage_path(path: Path) -> str | None:
    """POSIX path of ``path`` relative to ``media_root`` (never absolute, §2.1)."""
    try:
        return path.resolve().relative_to(media_root().resolve()).as_posix()
    except ValueError:
        logger.error("путь оказался вне каталога медиа: %s", path)
        return None


def _copy_thumbnail(
    source_attachment: MediaAttachment,
    folder: Path,
    target_stem: str,
) -> str | None:
    """Copy the source preview next to the new file; returns its relative path.

    The preview keeps the ``{stem}.thumb.jpg`` shape so
    :func:`~app.services.media.thumbnail_path_for` still matches it. A missing or
    unreadable preview is never fatal: the UI falls back to the full image.
    """
    thumbnail = str(getattr(source_attachment, "thumbnail_path", None) or "").strip()
    if not thumbnail:
        return None
    try:
        source = resolve_media_path(thumbnail)
    except ValueError as exc:
        logger.warning(
            "отклонён небезопасный путь миниатюры вложения %s: %s",
            getattr(source_attachment, "id", "?"),
            exc,
        )
        return None
    if not source.is_file():
        logger.info(
            "миниатюра вложения %s отсутствует на диске: %s",
            getattr(source_attachment, "id", "?"),
            source,
        )
        return None

    destination = folder / f"{target_stem}{THUMBNAIL_SUFFIX}"
    if not _copy_file(source, destination):
        return None
    return _relative_storage_path(destination)


def copy_attachment(
    db: AsyncSession,
    source_attachment: MediaAttachment,
    *,
    message_id: int,
    chat_id: int,
    tg_msg_id: int | None = None,
    message: GroupMessage | None = None,
) -> MediaAttachment | None:
    """Duplicate one attachment (file **and** row) for a forwarded message.

    When ``message`` is supplied the new row is attached through the ORM
    relationship, not just via a bare foreign key: that keeps
    ``message.attachments`` populated in the calling session, so the create
    response can render the media without a second round-trip. ``message_id``
    stays authoritative for the column value.

    The bytes are copied into ``{media_root}/chats/{chat_id}/{message_id}/`` under
    a fresh, collision-free name derived from the original, and the preview is
    copied alongside. The returned row records only **relative POSIX** paths
    (CONTRACTS_V2 §2.1) and no ``tg_file_id``, because the file is a brand new
    upload for Telegram rather than a reference to the original one.

    ``tg_msg_id`` is accepted for call-site symmetry with the forwarding flow and
    deliberately ignored: the Telegram message id is assigned by the worker's ACK,
    not at copy time.

    Returns ``None`` — after logging — when the source file cannot be copied or is
    missing; the caller keeps the message and loses only that one file.
    """
    del tg_msg_id  # the copy is a new upload; ids arrive with the worker ACK

    label = getattr(source_attachment, "id", "?")
    raw_path = str(getattr(source_attachment, "storage_path", "") or "")
    try:
        source = resolve_media_path(raw_path)
    except ValueError as exc:
        logger.warning("отклонён небезопасный путь вложения %s: %s", label, exc)
        return None
    if not source.is_file():
        logger.warning("файл вложения %s отсутствует на диске: %s", label, source)
        return None

    folder = media_root() / MEDIA_ROOT_SUBDIR / str(int(chat_id)) / str(int(message_id))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("не удалось создать каталог %s: %s: %s", folder, type(exc).__name__, exc)
        return None

    file_name = str(
        getattr(source_attachment, "file_name", None) or source.name or _FALLBACK_FILE_NAME
    )
    on_disk_name = _unique_name(folder, file_name)
    destination = folder / on_disk_name
    if not _copy_file(source, destination):
        return None

    storage_path = _relative_storage_path(destination)
    if storage_path is None:  # pragma: no cover - destination is built from media_root
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    try:
        size_bytes = int(destination.stat().st_size)
    except OSError as exc:  # pragma: no cover - file vanished right after the copy
        logger.warning("не удалось прочитать размер копии %s: %s", destination, exc)
        return None

    thumbnail_path = _copy_thumbnail(source_attachment, folder, Path(on_disk_name).stem)

    declared_kind = str(getattr(source_attachment, "kind", None) or "").strip()
    kind = declared_kind if declared_kind else MessageKind.DOCUMENT.value

    mime_type = str(getattr(source_attachment, "mime_type", None) or "").strip() or None

    attachment = MediaAttachment(
        message_id=int(message_id),
        kind=kind,
        file_name=safe_file_name(file_name, fallback=_FALLBACK_FILE_NAME)[:255],
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=getattr(source_attachment, "width", None),
        height=getattr(source_attachment, "height", None),
        duration_seconds=getattr(source_attachment, "duration_seconds", None),
        storage_path=storage_path,
        thumbnail_path=thumbnail_path,
        tg_file_id=None,
    )
    # The row is created with an explicit ``message_id``: appending through the
    # relationship would need ``message.attachments`` loaded, and after the
    # ``flush()`` that assigned the id the collection is expired — appending to it
    # then silently drops the child instead of inserting it.
    db.add(attachment)
    logger.info(
        "вложение %s скопировано для сообщения %s: %s (%d байт, миниатюра=%s)",
        label,
        message_id,
        storage_path,
        size_bytes,
        thumbnail_path or "нет",
    )
    return attachment


def _iter_source_attachments(source_message: GroupMessage) -> list[MediaAttachment]:
    """Attachments of ``source_message`` as a plain list (never a lazy load).

    ``GroupMessage.attachments`` is ``lazy="selectin"``, so reading it on a row
    the caller loaded is free. The guard covers a detached instance whose
    collection was never loaded — under asyncio that raises
    ``MissingGreenlet`` — and reports it instead of aborting the whole forward.
    """
    try:
        return list(getattr(source_message, "attachments", None) or [])
    except Exception as exc:  # noqa: BLE001 - a lazy-load failure must not abort the forward
        logger.warning(
            "не удалось прочитать вложения сообщения %s: %s: %s",
            getattr(source_message, "id", "?"),
            type(exc).__name__,
            exc,
        )
        return []


def message_label(message: GroupMessage) -> str:
    """``chat_id:message_id`` label for logs (never the sender's name)."""
    return f"{getattr(message, 'chat_id', '?')}:{getattr(message, 'id', '?')}"


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


def _forwarded_kind(source_message: GroupMessage, copied: list[MediaAttachment]) -> str:
    """Kind of the forwarded message: the source kind while it still has media.

    A ``PHOTO`` source stays ``PHOTO``; a media source whose files could not be
    copied degrades to ``TEXT`` instead of advertising an attachment that does not
    exist (the UI would otherwise render an empty bubble).
    """
    declared = str(getattr(source_message, "kind", None) or "").strip() or MessageKind.TEXT.value
    if declared in _NON_TEXT_KINDS and copied:
        return declared
    if copied:
        first_kind = str(getattr(copied[0], "kind", None) or "").strip()
        if first_kind:
            return first_kind
    return MessageKind.TEXT.value


def _forwarded_caption(source_message: GroupMessage, copied: list[MediaAttachment]) -> str | None:
    """Caption for the new media message: the source caption, capped (§1.4)."""
    if not copied:
        return None
    caption = sanitize_telegram_text(str(getattr(source_message, "caption", None) or ""))
    if not caption:
        return None
    return caption[:MAX_CAPTION_LENGTH].rstrip() or None


async def _resolve_source_incident(
    db: AsyncSession,
    source_message: GroupMessage,
    target_chat: GroupChat,
    operator: Operator,
    new_message: GroupMessage,
) -> None:
    """Close the SLA incident that the source message started, if any is open.

    The forwarded content answers the client, so leaving the source chat's clock
    running would count a reply the operator did make. ``sla_engine`` matches the
    incident inside ``source_message.chat_id`` and only resolves a ``PENDING``
    one, so a forward that quotes nothing is a cheap no-op. Any failure is logged
    and swallowed: SLA bookkeeping must never block the forward.
    """
    source_id = getattr(source_message, "id", None)
    if source_id is None:
        return
    try:
        incident = await sla_engine.resolve_for_reply(
            db,
            int(source_message.chat_id),
            int(source_id),
            operator,
            new_message,
        )
    except Exception as exc:  # noqa: BLE001 - decorative bookkeeping
        logger.warning(
            "не удалось закрыть SLA-инцидент для сообщения %s: %s: %s",
            source_id,
            type(exc).__name__,
            exc,
        )
        return
    if incident is not None:
        new_message.sla_incident_id = getattr(incident, "id", None)
        logger.info(
            "пересылка сообщения %s закрыла SLA-инцидент %s (чат %s)",
            source_id,
            getattr(incident, "id", "?"),
            target_chat.id,
        )


async def forward_message(
    db: AsyncSession,
    *,
    source_message: GroupMessage,
    target_chat: GroupChat,
    operator: Operator,
    comment: str | None = None,
) -> GroupMessage:
    """Recreate ``source_message`` inside ``target_chat`` as the operator's own.

    Steps, each independently observable:

    1. validate (§1.2: ``422`` same chat, ``409`` deleted in Telegram);
    2. build the text (``comment`` + caption/text, sanitised, capped at
       :data:`MAX_TEXT_LENGTH`);
    3. insert the new :class:`~app.models.GroupMessage` — ``tg_msg_id=None``,
       ``sender_type='OPERATOR'``, ``forwarded_from=None`` (invariant §5.1) —
       with ``delivery_status='PENDING'`` for a real chat and ``'SENT'`` for a
       mock one;
    4. ``flush()`` so the row gets its id: the media folder is named after it and
       the router needs it for the Redis payload;
    5. copy every attachment (file **and** row), logging and skipping failures;
    6. set ``kind``/``caption`` from what was actually copied;
    7. bump ``target_chat.last_message_at`` and clear ``unread_count``;
    8. resolve the SLA incident quoted by the source (best effort).

    **Commits nothing** — the caller owns the transaction and must ``commit()``
    before publishing to Redis, exactly like the other write paths.

    Telegram's ``forward_messages`` is never modelled here: this service only ever
    yields a new row plus on-disk copies, which the worker sends as its own
    ``send_file`` / ``send_message``.
    """
    validate_forward(source_message, target_chat)

    text = build_forward_text(source_message, comment)
    now = utcnow()
    is_mock_chat = target_chat.tg_chat_id is None

    message = GroupMessage(
        chat_id=int(target_chat.id),
        tg_msg_id=None,
        sender_type=MessageSenderType.OPERATOR.value,
        sender_operator_id=getattr(operator, "id", None),
        sender_name=getattr(operator, "display_name", None),
        sender_tg_id=None,
        sender_username=None,
        text=text,
        reply_to_id=None,
        reply_to_tg_msg_id=None,
        phone=extract_phone(text),
        is_deleted_in_tg=False,
        deleted_at=None,
        sla_incident_id=None,
        delivery_status=(
            DeliveryStatus.SENT.value if is_mock_chat else DeliveryStatus.PENDING.value
        ),
        client_nonce=None,
        kind=MessageKind.TEXT.value,
        caption=None,
        # INVARIANT (CONTRACTS_V3 §1.4/§5.1): the original sender must never leak
        # into the target chat, so this is always ``None`` — never derived from the
        # source row, never a placeholder string.
        forwarded_from=None,
        created_at=now,
    )
    db.add(message)

    try:
        await db.flush()
    except Exception as exc:  # noqa: BLE001 - surfaced as a contract status code
        logger.exception(
            "не удалось создать пересланное сообщение в чате %s (источник %s)",
            target_chat.id,
            message_label(source_message),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Не удалось сохранить пересланное сообщение",
        ) from exc

    sources = _iter_source_attachments(source_message)
    copied: list[MediaAttachment] = []
    for source_attachment in sources:
        try:
            duplicate = copy_attachment(
                db,
                source_attachment,
                message_id=int(message.id),
                chat_id=int(target_chat.id),
                tg_msg_id=int(message.id),
                message=message,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the forward
            logger.exception(
                "неожиданная ошибка при копировании вложения %s для сообщения %s: %s",
                getattr(source_attachment, "id", "?"),
                message.id,
                exc,
            )
            continue
        if duplicate is not None:
            copied.append(duplicate)

    message.kind = _forwarded_kind(source_message, copied)
    message.caption = _forwarded_caption(source_message, copied)

    target_chat.last_message_at = message.created_at
    target_chat.unread_count = 0

    await _resolve_source_incident(db, source_message, target_chat, operator, message)

    if len(copied) != len(sources):
        logger.warning(
            "пересылка %s в чат %s: скопировано %d из %d вложений",
            message_label(source_message),
            target_chat.id,
            len(copied),
            len(sources),
        )
    logger.info(
        "сообщение %s переслано в чат %s как сообщение %s (%d вложений, оператор %s)",
        message_label(source_message),
        target_chat.id,
        message.id,
        len(copied),
        getattr(operator, "username", "?"),
    )
    return message


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def _absolute_attachment_path(storage_path: str | None) -> str | None:
    """Absolute on-disk path for the worker from a relative ``storage_path``.

    Rows store relative paths (§2.1 of CONTRACTS_V2) while the worker needs a real
    one; an unsafe or vanished file degrades to ``None`` so the worker falls back
    to ``send_message`` instead of failing the whole command.
    """
    raw = str(storage_path or "").strip()
    if not raw:
        return None
    absolute = Path(raw)
    if absolute.is_absolute():
        if absolute.is_file():
            return str(absolute)
        logger.warning("файл для публикации отсутствует на диске: %s", absolute)
        return None
    try:
        path = resolve_media_path(raw)
    except ValueError as exc:
        logger.warning("отклонён небезопасный путь вложения при публикации: %s", exc)
        return None
    if not path.is_file():
        logger.warning("файл для публикации отсутствует на диске: %s", path)
        return None
    return str(path)


def _first_attachment_path(message: GroupMessage) -> tuple[str | None, str | None]:
    """``(absolute_path, kind)`` of the first attachment of ``message``.

    Used when the caller has not resolved the copy itself: the row belongs to the
    session that created it, so one read-committed query is enough, and any
    failure just yields ``(None, None)``.
    """
    attachments = _iter_source_attachments(message)
    if attachments:
        first = attachments[0]
    else:
        session = object_session(message)
        if session is None:
            return None, None
        try:
            rows = list(session.new) + list(session.identity_map.values())
            for row in rows:
                if isinstance(row, MediaAttachment) and int(getattr(row, "message_id", -1)) == int(
                    getattr(message, "id", -1)
                ):
                    first = row
                    break
            else:
                return None, None
        except Exception as exc:  # noqa: BLE001 - resolution is optional
            logger.warning("не удалось получить вложения сообщения %s: %s", message_label(message), exc)
            return None, None
    kind = str(getattr(first, "kind", None) or "").strip().upper() or None
    return _absolute_attachment_path(str(getattr(first, "storage_path", "") or "")), kind


async def publish_forward_command(
    chat: GroupChat,
    message: GroupMessage,
    operator: Operator,
    *,
    source_chat_id: int,
    source_message_id: int,
    attachment_path: str | None,
    attachment_kind: str | None,
) -> str:
    """Publish the §1.3 ``tg_forward`` command; returns the ``cmd_id`` used.

    The worker receives the **target** chat plus the on-disk copy of the file and
    sends it as an ordinary message (``send_file(path, caption=text)`` or
    ``send_message(text)``) — never a Telegram forward. ``attachment_path`` may be
    either the relative ``storage_path`` of the copied row or an absolute path; a
    relative one is resolved against ``media_root`` here so the caller never has
    to know the media layout. A missing file clears both ``attachment_path`` and
    ``attachment_kind``: a text-only send must not claim an attachment.

    A Redis outage is logged and swallowed (the REST call still succeeds and the
    row stays ``pending``), mirroring the outgoing publisher in the messages
    router.
    """
    cmd_id = str(uuid.uuid4())

    resolved_path = _absolute_attachment_path(attachment_path)
    kind = str(attachment_kind or "").strip().upper() or None
    if resolved_path is None:
        kind = None
    elif kind is None:
        resolved_path, kind = _first_attachment_path(message)

    payload: dict[str, object] = {
        "cmd_id": cmd_id,
        "source_chat_id": int(source_chat_id),
        "source_message_id": int(source_message_id),
        "target_chat_id": int(chat.id),
        "target_tg_chat_id": chat.tg_chat_id,
        "message_id": int(message.id),
        "text": message.text,
        "attachment_path": resolved_path,
        "attachment_kind": kind,
        "reply_to_tg_msg_id": None,
        "operator_username": getattr(operator, "username", None),
        "via_crm": True,
    }

    try:
        from app.redis_bus import publish

        await publish(CH_TG_FORWARD, payload)
    except Exception as exc:  # noqa: BLE001 - the REST call must still succeed
        logger.warning(
            "не удалось опубликовать %s для сообщения %s (чат %s): %s: %s",
            CH_TG_FORWARD,
            getattr(message, "id", "?"),
            getattr(chat, "id", "?"),
            type(exc).__name__,
            exc,
        )
    return cmd_id

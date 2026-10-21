"""Message text editing — the asymmetry between Telegram and CRM (CONTRACTS_V3 §2).

Business rules
--------------
CONTRACTS_V3 §2.1 makes editing deliberately **asymmetric**, and that asymmetry
is the entire point of this module:

===========================  =====================================================
who edited                   what happens
===========================  =====================================================
**Telegram** (``tg_edits``)  the CRM text is updated **and** the previous version
                             is preserved forever in ``message_edits``. A client
                             may rewrite what they promised, so "what was said
                             first" must stay visible to the operator.
**CRM** (an operator)        the text is updated in the CRM *and* pushed to
                             Telegram, but **no history row is written** — it is
                             our own correction, and the operator does not need to
                             audit themselves (invariant §5.3).
===========================  =====================================================

Consequences implemented below:

* :func:`apply_telegram_edit` locks the message row with ``SELECT ... FOR UPDATE``
  before reading its text. Two Telegram edits arriving on two consumer
  connections therefore serialise: the second one records the text the first one
  committed, so the history never loses a version. Without the lock both would
  read the same ``old_text`` and one version would silently disappear.
* :func:`apply_crm_edit` never touches ``MessageEdit``. It only moves the text
  and resets ``delivery_status`` to ``pending`` (invariant §5.4): the edit is
  still in flight to Telegram, and the ``tg_outgoing_ack`` that follows is what
  turns the badge back to ``sent``.
* A message deleted in Telegram (``is_deleted_in_tg``) cannot be edited — §5.5.
* Re-applying the **same** text is a no-op: no duplicate history row, and the
  message itself is returned so the caller can still broadcast idempotently.

Neither write coroutine commits — the caller owns the transaction boundary
(a router for CRM edits, the ``tg_edits`` handler for Telegram edits), exactly
like :mod:`app.services.sla_engine`'s public coroutines.

Redis (backend -> worker)
-------------------------
:func:`publish_edit_command` pushes ``tg_outgoing`` with the extra
``edit_tg_msg_id`` field (CONTRACTS_V3 §2.4). Publishing failures are **logged
and swallowed**: the database write is already committed and the REST response
must not depend on a Redis round trip.

Database-agnostic: every statement used here works on PostgreSQL (production) and
SQLite (automated tests). ``with_for_update()`` is silently ignored by SQLite,
which is acceptable because SQLite serialises writers itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DeliveryStatus, EditSource
from app.models import GroupMessage, MessageEdit, Operator
from app.utils import coerce_aware_utc, extract_phone, iso_z, sanitize_telegram_text, utcnow

logger = logging.getLogger("tgdesk.edits")

__all__ = [
    "DEFAULT_EDIT_HISTORY_LIMIT",
    "EVT_MESSAGE_EDITED",
    "MAX_EDIT_ACTOR_NAME_LENGTH",
    "MAX_EDIT_HISTORY_LIMIT",
    "apply_crm_edit",
    "apply_telegram_edit",
    "edit_message_id",
    "edit_to_dict",
    "edits_as_dicts",
    "find_locked_edit_target",
    "is_telegram_edit",
    "list_edits",
    "publish_edit_command",
    "record_edit",
]

#: ``MESSAGE_EDITED`` — CONTRACTS_V3 §2.6. Declared here so the WS layer does not
#: have to grow a constant for it.
EVT_MESSAGE_EDITED: Final[str] = "MESSAGE_EDITED"

#: Default page size of :func:`list_edits` (the history of one message).
DEFAULT_EDIT_HISTORY_LIMIT: Final[int] = 50

#: Upper bound for :func:`list_edits`, so a hostile ``?limit=1000000`` cannot ask
#: the database for an unbounded result set.
MAX_EDIT_HISTORY_LIMIT: Final[int] = 500

#: ``message_edits.actor_name`` is ``VARCHAR(255)``; a longer Telegram display
#: name must be truncated instead of making the INSERT fail.
MAX_EDIT_ACTOR_NAME_LENGTH: Final[int] = 255


# ---------------------------------------------------------------------------
# Small coercions
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int | None:
    """Best-effort integer coercion for ids coming from Telegram JSON."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_text(value: Any) -> str:
    """Normalise an edit payload to the text the CRM stores.

    Telegram sends ``None`` for a message that became empty, so ``None`` must
    become ``""`` rather than the string ``"None"``. The result goes through
    :func:`~app.utils.sanitize_telegram_text` for exactly the same reason
    ``tg_ingest.handle_incoming`` does: control characters and >4096-character
    bodies would otherwise be stored verbatim.
    """
    if value is None:
        return ""
    return sanitize_telegram_text(value if isinstance(value, str) else str(value))


def _as_actor_name(value: Any) -> str | None:
    """Trim and cap an ``actor_name`` to the column width (``None`` when empty)."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_EDIT_ACTOR_NAME_LENGTH]


def _as_datetime(value: Any) -> datetime | None:
    """Parse ``edited_at`` into an aware UTC datetime (``None`` when unusable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return coerce_aware_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            return coerce_aware_utc(datetime.fromisoformat(raw))
        except ValueError:
            logger.debug("edits: unparseable edited_at %r, falling back to now()", value)
            return None
    return None


def _operator_id(operator: Any) -> int | None:
    """``operator.id`` as a positive ``int`` (``None`` when the object carries none).

    A missing id is not fatal here: the edit itself is what matters, and the id
    is only used to label the outgoing command and the log line.
    """
    value = _to_int(getattr(operator, "id", None))
    return value if value is not None and value > 0 else None


def _operator_label(operator: Any) -> str:
    """Human label for logs: ``username`` else ``display_name`` else ``#id``."""
    for attribute in ("username", "display_name"):
        raw = getattr(operator, attribute, None)
        if raw is not None:
            text = str(raw).strip()
            if text:
                return text
    identifier = _operator_id(operator)
    return f"#{identifier}" if identifier is not None else "unknown operator"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def _load_message_by_tg_id(
    db: AsyncSession,
    *,
    chat_id: int,
    tg_msg_id: int,
) -> GroupMessage | None:
    """Newest message of ``chat_id`` carrying ``tg_msg_id``, locked for update.

    ``ORDER BY id DESC LIMIT 1`` mirrors ``tg_ingest``: a redelivered
    ``tg_incoming`` frame could in principle have produced two rows for one
    Telegram message, and the newest one is the live one.
    """
    stmt = (
        select(GroupMessage)
        .where(
            GroupMessage.chat_id == int(chat_id),
            GroupMessage.tg_msg_id == int(tg_msg_id),
        )
        .order_by(GroupMessage.id.desc())
        .limit(1)
        .with_for_update()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_locked_edit_target(
    db: AsyncSession,
    *,
    chat_id: int,
    tg_msg_id: int,
) -> GroupMessage | None:
    """Public ``FOR UPDATE`` lookup used by ``tg_ingest.handle_edit``."""
    return await _load_message_by_tg_id(db, chat_id=int(chat_id), tg_msg_id=int(tg_msg_id))


async def list_edits(
    db: AsyncSession,
    message_id: int,
    *,
    limit: int = DEFAULT_EDIT_HISTORY_LIMIT,
) -> list[MessageEdit]:
    """Edit history of ``message_id``, oldest first (CONTRACTS_V3 §2.3).

    Ordered by ``created_at ASC, id ASC``: two edits recorded inside the same
    second still keep their real order, because ``id`` is monotonic. ``limit`` is
    clamped to ``[1, MAX_EDIT_HISTORY_LIMIT]`` so a hostile query parameter cannot
    ask for an unbounded result set.

    Returns an empty list for a message that has no recorded history — including
    every message that was only ever edited from the CRM (§5.3).
    """
    key = _to_int(message_id)
    if key is None or key <= 0:
        logger.debug("list_edits: ignoring unusable message_id %r", message_id)
        return []
    size = _to_int(limit)
    if size is None or size <= 0:
        size = DEFAULT_EDIT_HISTORY_LIMIT
    size = min(size, MAX_EDIT_HISTORY_LIMIT)
    try:
        rows = (
            await db.execute(
                select(MessageEdit)
                .where(MessageEdit.message_id == key)
                .order_by(MessageEdit.created_at.asc(), MessageEdit.id.asc())
                .limit(size)
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - a history read must never break the message view
        logger.warning("list_edits(%s) failed: %s: %s", key, type(exc).__name__, exc)
        return []
    return list(rows)


def edit_to_dict(edit: Any) -> dict[str, Any]:
    """Serialise one ``message_edits`` row (CONTRACTS_V3 §2.3 ``MessageEditOut``).

    The shape is frozen: ``{id, message_id, source, old_text, new_text,
    operator_id, actor_name, created_at}`` with ``created_at`` as ISO-8601 ending
    in ``Z``. Deliberately tolerant of partial objects (a test double, a detached
    instance) so a missing attribute degrades to ``None`` instead of raising
    inside a WebSocket broadcast.
    """
    created_at = getattr(edit, "created_at", None)
    source = getattr(edit, "source", None)
    return {
        "id": _to_int(getattr(edit, "id", None)),
        "message_id": _to_int(getattr(edit, "message_id", None)),
        "source": str(getattr(source, "value", source)) if source is not None else None,
        "old_text": getattr(edit, "old_text", None) or "",
        "new_text": getattr(edit, "new_text", None) or "",
        "operator_id": _to_int(getattr(edit, "operator_id", None)),
        "actor_name": getattr(edit, "actor_name", None),
        "created_at": iso_z(created_at if isinstance(created_at, datetime) else None),
    }


def edit_message_id(edit: Any) -> int | None:
    """``message_id`` of an edit row (``None`` when the object carries none)."""
    return _to_int(getattr(edit, "message_id", None))


def edits_as_dicts(edits: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Serialise a sequence of edit rows, skipping unusable entries."""
    return [edit_to_dict(edit) for edit in (edits or ()) if edit is not None]


def is_telegram_edit(edit: Mapping[str, Any] | None) -> bool:
    """``True`` when a serialised edit came from Telegram (the audited source)."""
    if not isinstance(edit, Mapping):
        return False
    return str(edit.get("source") or "").upper() == EditSource.TELEGRAM.value


# ---------------------------------------------------------------------------
# History writer
# ---------------------------------------------------------------------------


def record_edit(
    message: GroupMessage,
    *,
    source: EditSource | str,
    old_text: str,
    new_text: str,
    operator: Operator | None = None,
    operator_id: int | None = None,
    actor_name: str | None = None,
    created_at: datetime | None = None,
) -> MessageEdit:
    """Build (but do **not** add) a ``MessageEdit`` row for ``message``.

    Split out of :func:`apply_telegram_edit` so the field mapping lives in one
    place. Callers must ``db.add(...)`` the returned object. ``source`` is
    normalised by the model's ``@validates`` hook, so an enum member and its raw
    string are equivalent.
    """
    identifier = _operator_id(operator) if operator is not None else _to_int(operator_id)
    return MessageEdit(
        message_id=int(message.id),
        source=str(getattr(source, "value", source)),
        old_text=old_text,
        new_text=new_text,
        operator_id=identifier,
        actor_name=_as_actor_name(actor_name),
        created_at=coerce_aware_utc(created_at) if created_at is not None else utcnow(),
    )


# ---------------------------------------------------------------------------
# Telegram -> CRM (audited: the old version is preserved)
# ---------------------------------------------------------------------------


async def apply_telegram_edit(
    db: AsyncSession,
    *,
    chat_id: int,
    tg_msg_id: int,
    new_text: str,
    actor_name: str | None = None,
    edited_at: datetime | None = None,
) -> tuple[GroupMessage, MessageEdit | None] | None:
    """Mirror a Telegram-side edit onto the stored message (CONTRACTS_V3 §2.1).

    Steps (each one is a contract clause):

    1. locate the message by ``chat_id`` + ``tg_msg_id`` **with**
       ``SELECT ... FOR UPDATE`` — two concurrent edits of one message then
       serialise instead of both recording the same ``old_text``;
    2. ``None`` when the message is unknown (the worker may report an edit for a
       message whose ``tg_incoming`` frame has not been consumed yet) — logged at
       ``info``, never raised, so the Redis consumer survives;
    3. ``None`` when ``is_deleted_in_tg`` — editing a deleted message is
       meaningless (§5.5);
    4. when the normalised text equals the stored text the call is
       **idempotent**: nothing is written, no history row is created, and
       ``(message, None)`` is returned so the caller can still broadcast the
       current state;
    5. otherwise ``text`` / ``phone`` / ``updated_at`` are updated and a
       ``source=TELEGRAM`` :class:`~app.models.MessageEdit` row carrying the
       **old** text is added.

    The caller commits: this coroutine only ``flush()``-es, so the text update and
    its history row land in the *same* transaction (a crash between the two would
    otherwise lose the version the operator needs to see).

    Returns ``(message, edit)`` — ``edit`` is ``None`` for a no-op — or ``None``
    when there was nothing to edit.
    """
    chat_key = _to_int(chat_id)
    tg_key = _to_int(tg_msg_id)
    if chat_key is None or tg_key is None:
        logger.warning(
            "apply_telegram_edit: unusable chat_id/tg_msg_id (%r/%r), ignored", chat_id, tg_msg_id
        )
        return None

    message = await _load_message_by_tg_id(db, chat_id=chat_key, tg_msg_id=tg_key)
    if message is None:
        logger.info(
            "apply_telegram_edit: tg_msg_id=%s is not stored in chat %s yet, ignored",
            tg_key,
            chat_key,
        )
        return None

    if bool(message.is_deleted_in_tg):
        logger.info(
            "apply_telegram_edit: message %s (tg_msg_id=%s) is deleted in Telegram, edit ignored",
            message.id,
            tg_key,
        )
        return None

    text = _as_text(new_text)
    old_text = message.text or ""
    actor = _as_actor_name(actor_name)
    stamp = _as_datetime(edited_at) or utcnow()

    if old_text == text:
        # Redelivered frame or an "edit" with an identical body: nothing changed,
        # so no history row is created. The row is still returned so the caller
        # broadcasts the same state instead of silently dropping the event.
        logger.debug(
            "apply_telegram_edit: message %s already carries the edited text, no history row",
            message.id,
        )
        return message, None

    message.text = text
    message.phone = extract_phone(text)
    message.updated_at = stamp

    edit = record_edit(
        message,
        source=EditSource.TELEGRAM,
        old_text=old_text,
        new_text=text,
        operator=None,
        operator_id=None,
        actor_name=actor,
        created_at=stamp,
    )
    db.add(edit)
    await db.flush()

    logger.info(
        "apply_telegram_edit: message %s (chat %s) edited in Telegram%s, previous version kept "
        "(edit %s)",
        message.id,
        chat_key,
        f" by {actor}" if actor else "",
        getattr(edit, "id", "?"),
    )
    return message, edit


# ---------------------------------------------------------------------------
# CRM -> Telegram (never audited)
# ---------------------------------------------------------------------------


async def apply_crm_edit(
    db: AsyncSession,
    message: GroupMessage,
    *,
    new_text: str,
    operator: Operator,
) -> GroupMessage:
    """Apply an operator's correction in the CRM (CONTRACTS_V3 §2.1 / §5.3 / §5.4).

    Three things happen and **nothing else**:

    1. ``text`` / ``phone`` / ``updated_at`` are updated;
    2. ``delivery_status`` goes back to ``pending`` — the edit is now in flight to
       Telegram and only the upcoming ``tg_outgoing_ack`` may clear it (§5.4);
    3. **no ``MessageEdit`` row is written.** This is our own edit: recording it
       would fill the Telegram-only history with CRM corrections and make the
       "Изменено из Telegram" panel lie about its source.

    The text is **not** sanitised: an operator legitimately types trailing
    newlines and the router validates the body length. Only ``None`` is
    normalised to ``""`` so ``group_messages.text`` (``NOT NULL``) stays valid.

    Does not commit — the router owns the transaction.
    """
    text = "" if new_text is None else (new_text if isinstance(new_text, str) else str(new_text))
    old_text = message.text or ""
    stamp = utcnow()

    message.text = text
    message.phone = extract_phone(text)
    message.updated_at = stamp
    message.delivery_status = DeliveryStatus.PENDING.value
    await db.flush()

    logger.info(
        "apply_crm_edit: operator %s edited message %s (chat %s); no history row written, "
        "delivery_status=%s",
        _operator_label(operator),
        message.id,
        message.chat_id,
        message.delivery_status,
    )
    if old_text == text:
        logger.debug("apply_crm_edit: message %s text is unchanged", message.id)
    return message


# ---------------------------------------------------------------------------
# Redis: backend -> worker (``tg_outgoing`` with ``edit_tg_msg_id``)
# ---------------------------------------------------------------------------


async def publish_edit_command(
    message: GroupMessage,
    chat: Any,
    operator: Any,
) -> str:
    """Publish an ``edit_message`` command to the worker (CONTRACTS_V3 §2.4).

    The ``tg_outgoing`` frame is the standard one plus two fields:
    ``edit_tg_msg_id`` (the Telegram id to edit — when set the worker calls
    ``client.edit_message`` instead of ``send_message``) and ``via_crm`` (so the
    worker's own ``tg_edits`` echo can be recognised and not re-mirrored). The
    ACK stays identical, with ``tg_msg_id`` = the edited id.

    Returns the ``cmd_id`` that was published so a caller can correlate it with
    the ack. **Never raises**: the database write is already committed by the
    time we get here, so a Redis outage must degrade to "text changed in the CRM,
    not in Telegram" (logged; surfaced by ``delivery_status`` staying ``pending``)
    rather than a ``500`` that would make the operator retry an edit that *did*
    succeed — and then write it twice.

    ``chat``/``operator`` are accepted as ``Any`` on purpose: this function is
    called from the router with ORM rows and from tests with lightweight
    stand-ins, and it only reads ``chat.id`` / ``chat.tg_chat_id`` /
    ``operator.username``.
    """
    cmd_id = str(uuid4())

    chat_id = _to_int(getattr(chat, "id", None))
    if chat_id is None:
        chat_id = _to_int(getattr(message, "chat_id", None))

    tg_chat_id = _to_int(getattr(chat, "tg_chat_id", None))

    edit_tg_msg_id = _to_int(getattr(message, "tg_msg_id", None))
    if edit_tg_msg_id is None:
        logger.warning(
            "publish_edit_command: message %s has no tg_msg_id — the worker cannot edit it",
            getattr(message, "id", "?"),
        )

    username = getattr(operator, "username", None)
    username = str(username).strip() if username is not None else ""

    text = getattr(message, "text", None)
    payload: dict[str, Any] = {
        "cmd_id": cmd_id,
        "chat_id": chat_id,
        "tg_chat_id": tg_chat_id,
        "message_id": _to_int(getattr(message, "id", None)),
        "text": "" if text is None else text,
        "edit_tg_msg_id": edit_tg_msg_id,
        "operator_username": username or None,
        "via_crm": True,
    }

    try:
        from app.redis_bus import CH_TG_OUTGOING, publish
    except Exception as exc:  # noqa: BLE001 - an import failure must not break REST either
        logger.error(
            "publish_edit_command: redis_bus unavailable (%s: %s), edit stays CRM-only",
            type(exc).__name__,
            exc,
        )
        return cmd_id

    try:
        await publish(CH_TG_OUTGOING, payload)
    except Exception as exc:  # noqa: BLE001 - the REST response must not depend on Redis
        logger.warning(
            "publish_edit_command: publishing to tg_outgoing failed (%s: %s) for message %s",
            type(exc).__name__,
            exc,
            payload["message_id"],
        )
        return cmd_id

    logger.info(
        "publish_edit_command: cmd %s queued for message %s (edit_tg_msg_id=%s) by %s",
        cmd_id,
        payload["message_id"],
        edit_tg_msg_id,
        username or "unknown operator",
    )
    return cmd_id

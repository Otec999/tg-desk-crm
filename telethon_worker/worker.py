#!/usr/bin/env python3
"""TG-Desk CRM — Telethon <-> Redis bridge worker.

Runs as a **long-lived process** on the operator's Windows host (native
Telethon user account), watches the corporate Telegram groups and bridges
events to the FastAPI backend over Redis pub/sub, and pushes operator
replies back into Telegram.

Channel map (CONTRACTS.md §5 + CONTRACTS_V2.md §4 — field names are frozen)::

    tg_incoming       worker -> backend   NEW_MESSAGE (with ``media``/``caption``)
    tg_outgoing       backend -> worker   send request (text and/or attachment)
    tg_outgoing_ack   worker -> backend   delivery result
    tg_deleted        worker -> backend   MESSAGE_DELETED
    tg_edits          worker -> backend   MESSAGE_EDITED
    tg_reactions      worker -> backend   REACTION_UPDATE (CONTRACTS_V2 §4)
    tg_delete_out     backend -> worker   delete our message in Telegram (CONTRACTS_V5 §1.4)

Usage::

    pip install -r requirements.txt
    python worker.py                  # normal run (foreground, Ctrl+C stops)
    python worker.py --login          # first run: create a StringSession
    python worker.py --dry-run        # smoke-test credentials, no Redis writes
    python worker.py --list-chats     # print dialogs we would monitor, exit
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Third-party imports are guarded so that `--help` still works on a broken venv.
# --------------------------------------------------------------------------- #

try:
    from telethon import TelegramClient, events, utils as tg_utils
    from telethon.errors import (
        FloodWaitError,
        MessageDeleteForbiddenError,
        MessageNotModifiedError,
        RPCError,
    )
    from telethon.sessions import StringSession
    # CONTRACTS_V5 §1.6: deleting our own message in a supergroup/forum needs the
    # ``channels`` flavour of ``DeleteMessages`` — ``channel=<peer>, id=[msg]``.
    # The ``messages`` variant addresses private chats/basic groups and is not used.
    from telethon.tl.functions.channels import DeleteMessagesRequest
    # CONTRACTS_V4 §2: setting a reaction needs ``messages.SendReaction``; clearing is
    # the same RPC with an empty ``reaction`` list.
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        Channel,
        Chat,
        MessageMediaDocument,
        MessageMediaGeo,
        MessageMediaGeoLive,
        MessageMediaPhoto,
        MessageMediaPoll,
        MessageMediaUnsupported,
        MessageMediaVenue,
        MessageMediaWebPage,
        PeerChannel,
        PeerChat,
        PeerUser,
        ReactionEmoji,
        UpdateMessageReactions,
        User,
    )
except Exception as exc:  # pragma: no cover - import-time environment error
    sys.stderr.write(
        "FATAL: Telethon is not installed or is broken.\n"
        f"       {type(exc).__name__}: {exc}\n"
        "       Run:  pip install -r telethon_worker/requirements.txt\n"
    )
    raise SystemExit(2) from exc

try:
    import redis.asyncio as aioredis
    from redis.exceptions import RedisError
except Exception as exc:  # pragma: no cover - import-time environment error
    sys.stderr.write(
        "FATAL: redis-py is not installed or is broken.\n"
        f"       {type(exc).__name__}: {exc}\n"
        "       Run:  pip install -r telethon_worker/requirements.txt\n"
    )
    raise SystemExit(2) from exc

try:  # orjson is optional at runtime: json is a perfectly fine fallback.
    import orjson

    _dumps: Callable[[Any], str] = lambda obj: orjson.dumps(obj).decode("utf-8")
except Exception:  # pragma: no cover - optional dependency
    orjson = None  # type: ignore[assignment]

    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


try:
    import cryptg  # noqa: F401  (import side effect: activates the C crypto)
    _CRYPTG_AVAILABLE = True
except Exception:  # pragma: no cover - optional native speedup
    _CRYPTG_AVAILABLE = False


try:
    from config import REPO_ROOT, WorkerConfig, load_config
except Exception:  # pragma: no cover - allows `python -m telethon_worker.worker`
    from telethon_worker.config import (  # type: ignore[no-redef]
        REPO_ROOT,
        WorkerConfig,
        load_config,
    )

# ``media`` is the sibling module that owns the download/thumbnail logic
# (CONTRACTS_V2 §5). Imported both ways for the same reason as ``config``.
try:
    import media as media_store
except Exception:  # pragma: no cover - package-style import
    from telethon_worker import media as media_store  # type: ignore[no-redef]


# --------------------------------------------------------------------------- #
# Constants — CONTRACTS.md §5. Do not rename.
# --------------------------------------------------------------------------- #

CHANNEL_INCOMING: str = "tg_incoming"
CHANNEL_OUTGOING: str = "tg_outgoing"
CHANNEL_OUTGOING_ACK: str = "tg_outgoing_ack"
CHANNEL_DELETED: str = "tg_deleted"
CHANNEL_EDITS: str = "tg_edits"
#: NEW (CONTRACTS_V2 §4): reactions, worker -> backend.
CHANNEL_REACTIONS: str = "tg_reactions"
#: NEW (CONTRACTS_V3 §1.3): cross-chat forward command, backend -> worker.
#: The message is **re-sent** as our own text/attachment, never Telegram-
#: forwarded (see :meth:`TelethonWorker._send_forward_copy`).
CHANNEL_FORWARD: str = "tg_forward"
#: NEW (CONTRACTS_V4 §2): mirror an operator's CRM reaction onto Telegram,
#: backend -> worker. Telegram keeps one reaction per account, so this sets or
#: clears the reaction of OUR account only (see
#: :meth:`TelethonWorker._handle_reaction_command`).
CHANNEL_REACTION_OUT: str = "tg_reaction_out"
#: NEW (CONTRACTS_V5 §1.4): delete one of our own messages in Telegram, backend ->
#: worker. The row is acked on ``tg_outgoing_ack`` with ``cmd_kind="DELETE"``
#: (see :meth:`TelethonWorker._handle_delete_command`).
CHANNEL_DELETE_OUT: str = "tg_delete_out"

KIND_NEW_MESSAGE: str = "NEW_MESSAGE"
KIND_MESSAGE_DELETED: str = "MESSAGE_DELETED"
KIND_MESSAGE_EDITED: str = "MESSAGE_EDITED"
KIND_REACTION_UPDATE: str = "REACTION_UPDATE"

REDIS_STATUS_KEY: str = "worker:status"
REDIS_HEARTBEAT_KEY: str = "worker:heartbeat"

#: Telegram limits a single message body to 4096 characters (UTF-16 units).
TG_MAX_MESSAGE_LENGTH: int = 4096

#: Caption ceiling: Telegram itself allows 1024 characters for a caption and
#: ``group_messages.caption`` is ``Text`` with the same limit (CONTRACTS_V2
#: §2.4). Longer captions are truncated by the worker, otherwise the backend
#: would reject the row.
TG_MAX_CAPTION_LENGTH: int = 1024

#: The only reactions the CRM supports (CONTRACTS_V2 §1 ``ReactionEmoji``).
#: Anything else Telegram reports (custom emoji, ``ReactionCustomEmoji``, paid
#: reactions) is ignored instead of being mapped onto an enum value the
#: backend does not know.
SUPPORTED_REACTIONS: frozenset[str] = frozenset({"👍", "❤️", "🔥", "👀", "👏", "🙏"})

#: ``:tl:`ReactionEmoji`.emoticon`` -> contract emoji. Telegram clients render
#: ``❤️`` from the bare code point ``U+2764`` depending on the font, so the
#: variation-selector-less form is normalised instead of being dropped.
_REACTION_ALIASES: dict[str, str] = {
    "❤": "❤️",
    "\u2764\ufe0e": "❤️",
}

#: How many attachments of one message are downloaded concurrently. Albums
#: are rare beyond 10 items; the semaphore keeps a burst of photos from
#: saturating the single Telethon connection used for the control messages.
MAX_CONCURRENT_MEDIA_DOWNLOADS: int = 4

#: Echo suppression window: a CRM-originated message must not be re-ingested
#: from ``events.NewMessage`` (the backend already stores it as outgoing).
ECHO_WINDOW_SECONDS: float = 30.0

#: How long ``FloodWaitError`` may block our outgoing loop (telegram can ask
#: for hours; the backend fails the message and the operator retries).
FLOOD_WAIT_CAP_SECONDS: int = 300

#: Redis connect budget at startup.
REDIS_CONNECT_BUDGET_SECONDS: int = 30

#: Hard cap for a single outbound send (telethon retries internally).
SEND_TIMEOUT_SECONDS: int = 90

#: Budget for downloading one attachment. ``SEND_TIMEOUT_SECONDS`` is too
#: tight for a 50 MiB file on a slow uplink, and an unbounded download would
#: hold a media slot forever.
MEDIA_DOWNLOAD_TIMEOUT_SECONDS: int = 300

#: Telegram's caption limit for an outbound file (``send_file(caption=…)``).
TG_MAX_FILE_CAPTION_LENGTH: int = 1024

LOG = logging.getLogger("tg_worker")


# --------------------------------------------------------------------------- #
# Echo suppression
# --------------------------------------------------------------------------- #

#: ``(chat_key, text_hash) -> monotonic deadline``. Populated right after a
#: successful ``send_message`` from the CRM, so the ``NEW_MESSAGE`` event that
#: Telegram echoes back is dropped instead of being double-posted.
pending_crm_sends: dict[tuple[str, str], float] = {}


def _text_hash(text: str) -> str:
    """Stable short hash of a message body (echo suppression key)."""
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _chat_alias(value: Any) -> str:
    """Normalise a chat id (int) or username (``@foo`` / ``foo``) to one key."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip().lower()
    return text[1:] if text.startswith("@") else text


def register_pending_send(chat: Any, text: str, *, ttl: float = ECHO_WINDOW_SECONDS) -> None:
    """Remember that ``text`` was just sent to ``chat`` by the CRM.

    The entry expires after ``ttl`` seconds. Existing entries are never
    extended beyond their own deadline by a re-registration of the same text
    (identical consecutive messages still each get a fresh window).
    """
    key = (_chat_alias(chat), _text_hash(text or ""))
    pending_crm_sends[key] = time.monotonic() + max(1.0, ttl)


def consume_pending_send(chat: Any, text: str) -> bool:
    """Return ``True`` and drop the entry if this message is a CRM echo.

    Every Telegram chat the worker cares about is a group, therefore our own
    echoes always arrive with ``out=True`` and are filtered earlier; this
    check is the second line of defence for sessions where an "own message"
    is reported oddly (private saved messages, channel experiments).
    """
    prune_pending_sends()
    key = (_chat_alias(chat), _text_hash(text or ""))
    deadline = pending_crm_sends.get(key)
    if deadline is None:
        return False
    del pending_crm_sends[key]
    return time.monotonic() <= deadline


def prune_pending_sends() -> None:
    """Drop expired echo-suppression entries (keeps the dict tiny)."""
    now = time.monotonic()
    for key in [k for k, deadline in pending_crm_sends.items() if deadline <= now]:
        pending_crm_sends.pop(key, None)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def utc_iso(dt: Optional[datetime] = None) -> str:
    """ISO-8601 timestamp with an **offset** (CONTRACTS §5 wants ``+00:00``).

    Naive datetimes are treated as UTC. Telethon already returns aware UTC
    datetimes; this is defensive.
    """
    moment = dt if dt is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def to_int_or_none(value: Any) -> Optional[int]:
    """Best-effort ``int`` cast that never raises (``None`` on failure)."""
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pubsub_channel(message: Any) -> str:
    """Return the channel name of one ``redis.asyncio`` pub/sub message.

    ``pubsub.listen()`` yields ``{"type": str, "pattern": ..., "channel": ...,
    "data": ...}``. ``channel`` is ``bytes`` by default (``decode_responses``
    is off) but may already be ``str``; anything unexpected renders to ``""``
    so the caller falls back to the historical ``tg_outgoing`` behaviour.
    """
    try:
        channel = message.get("channel") if isinstance(message, dict) else None
    except Exception:  # pragma: no cover - exotic mapping
        return ""
    if isinstance(channel, bytes):
        return channel.decode("utf-8", "replace")
    return channel if isinstance(channel, str) else ""


def peer_id_of(event_or_message: Any) -> Optional[int]:
    """Extract the numeric chat id from a telethon event or message."""
    for attr in ("chat_id", "peer_id"):
        try:
            value = getattr(event_or_message, attr, None)
        except Exception:  # pragma: no cover - exotic entity proxies
            value = None
        as_int = to_int_or_none(value)
        if as_int is not None:
            return as_int
    message = getattr(event_or_message, "message", None)
    if message is not None and message is not event_or_message:
        return peer_id_of(message)
    return None


def message_media_placeholder(message: Any) -> str:
    """Render a ``"[photo]"``-style placeholder for a media-only message.

    Never raises: unknown/unsupported media degrade to ``"[media]"``.
    """
    try:
        media = getattr(message, "media", None)
        if media is None:
            return ""
        if getattr(message, "video_note", None) is not None:
            return "[video message]"
        if getattr(message, "voice", None) is not None:
            return "[voice message]"
        if getattr(message, "video", None) is not None:
            return "[video]"
        if getattr(message, "photo", None) is not None:
            return "[photo]"
        if getattr(message, "audio", None) is not None:
            return "[audio]"
        if getattr(message, "sticker", None) is not None:
            return "[sticker]"
        if getattr(message, "gif", None) is not None:
            return "[animation]"
        if getattr(message, "contact", None) is not None:
            return "[contact]"
        if getattr(message, "geo", None) is not None:
            return "[location]"
        if getattr(message, "poll", None) is not None:
            return "[poll]"

        if isinstance(media, MessageMediaPhoto):
            return "[photo]"
        if isinstance(media, MessageMediaPoll):
            return "[poll]"
        if isinstance(media, (MessageMediaGeo, MessageMediaGeoLive)):
            return "[location]"
        if isinstance(media, MessageMediaVenue):
            return "[location]"
        if isinstance(media, MessageMediaWebPage):
            # A link preview alone: the raw URL lives in raw_text.
            return "[link]"
        if isinstance(media, MessageMediaUnsupported):
            return "[unsupported media]"
        if isinstance(media, MessageMediaDocument):
            document = getattr(media, "document", None)
            attributes = getattr(document, "attributes", None) or ()
            for attribute in attributes:
                name = type(attribute).__name__
                if name == "DocumentAttributeAudio":
                    return "[voice message]" if getattr(attribute, "voice", False) else "[audio]"
                if name == "DocumentAttributeVideo":
                    return "[video message]" if getattr(attribute, "round_message", False) else "[video]"
                if name == "DocumentAttributeSticker":
                    return "[sticker]"
                if name == "DocumentAttributeAnimated":
                    return "[animation]"
            return "[document]"
        return "[media]"
    except Exception:
        LOG.debug("media placeholder extraction failed", exc_info=True)
        return "[media]"


_MERCHANT_CODE_MARKER = "#mrc-"
_MERCHANT_TITLE_WORDS: frozenset[str] = frozenset(
    {"merchant", "mrc", "магазин", "мерчант", "team", "группа", "support", "chat", "чат"}
)
_MERCHANT_CODE_LEN = 32


def _sanitize_code(raw: str) -> str:
    """Uppercase, ``non-alphanumerics -> '-'``, trim leading/trailing dashes."""
    cleaned = []
    for ch in raw.strip().upper():
        if ch.isalnum():
            cleaned.append(ch)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    return "".join(cleaned).strip("-")[:_MERCHANT_CODE_LEN].strip("-")


def derive_merchant_code(
    *,
    title: Optional[str],
    chat_username: Optional[str] = None,
    known_codes: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Derive a ``MRC-XXX`` merchant code from a group title.

    Resolution order:

    1. explicit ``#MRC-XXX`` hashtag anywhere in the title
       (``"Merchant Alpha — Payouts #MRC-ALPHA"`` -> ``MRC-ALPHA``);
    2. if the title looks like a **merchant group** (starts with
       ``Merchant``/``Мерчант``/… ``MRC``/``#MRC``), the first business word
       is promoted: ``"Merchant Alpha — Payouts"`` -> ``MRC-ALPHA``,
       ``"Merchant Beta"`` -> ``MRC-BETA``;
    3. otherwise ``None`` — the backend must not invent codes for
       non-merchant groups.

    If the derived code is already listed in ``known_codes`` *as belonging to
    another group of the same first word* the caller is free to ignore it;
    we return it verbatim and let the backend bind it to its own chat row.
    """
    if not title:
        return None

    lowered = title.lower()

    marker_at = lowered.find(_MERCHANT_CODE_MARKER)
    if marker_at != -1:
        raw = title[marker_at + len(_MERCHANT_CODE_MARKER):]
        # Stop at whitespace / punctuation / next '#'
        for index, ch in enumerate(raw):
            if not (ch.isalnum() or ch in "-_"):
                raw = raw[:index]
                break
        code = _sanitize_code(raw)
        if code:
            return f"MRC-{code}" if not code.startswith("MRC") else code

    words = [w.strip("—–-–|:•·()[]{}«»\"'.,") for w in lowered.replace("—", " ").split()]
    words = [w for w in words if w]
    if not words:
        return None

    lead = words[0].lstrip("#@")
    merchant_hint = lead in _MERCHANT_TITLE_WORDS or "merchant" in lowered
    if lead.startswith("mrc") and len(lead) > 3:
        return _sanitize_code(lead)
    if not merchant_hint:
        return None

    for word in words[1:] if lead in _MERCHANT_TITLE_WORDS else words:
        if not word or word in _MERCHANT_TITLE_WORDS:
            continue
        if word.startswith("#"):
            continue
        code = _sanitize_code(word)
        if code:
            return f"MRC-{code}"
    return None


# --------------------------------------------------------------------------- #
# Media / caption helpers (CONTRACTS_V2 §2.4, §5)
# --------------------------------------------------------------------------- #


def trim_caption(text: Optional[str]) -> Optional[str]:
    """Clamp a caption to :data:`TG_MAX_CAPTION_LENGTH` (1024) characters.

    ``group_messages.caption`` is a ``Text`` column with the Telegram caption
    ceiling enforced by the schema owner; publishing anything longer makes the
    backend reject the whole row. The full body is still published in
    ``text`` — only the caption field is clamped (an ellipsis marks the cut).

    Returns:
        ``None`` when there is no caption at all (keeps the column NULL for
        text-only messages instead of storing an empty string).
    """
    if text is None:
        return None
    caption = str(text).strip()
    if not caption:
        return None
    if len(caption) <= TG_MAX_CAPTION_LENGTH:
        return caption
    return caption[: TG_MAX_CAPTION_LENGTH - 1].rstrip() + "…"


def message_kind(message: Any) -> str:
    """Return the ``MessageKind`` value of a Telethon message.

    Delegates to :mod:`media` (single source of truth for the classification
    rules, unit-tested in ``test_media.py``) and never raises:
    a classification error degrades to ``TEXT`` so the message is still
    ingested as text.
    """
    try:
        kind = media_store.detect_kind(message)
    except Exception:  # pragma: no cover - defensive, detect_kind is total
        LOG.exception("message kind detection failed; falling back to TEXT")
        return "TEXT"
    return kind or "TEXT"


def _caption_text(message: Any, raw_text: Optional[str]) -> Optional[str]:
    """Caption of a media message (``None`` for a pure text message)."""
    if getattr(message, "media", None) is None:
        return None
    return trim_caption(raw_text)


def _attachment_placeholder(records: list[dict[str, Any]]) -> str:
    """``"[photo] 2 attachments"``-style body for a caption-less media message."""
    if not records:
        return ""
    first = records[0]
    kind = str(first.get("kind") or "OTHER")
    name = first.get("file_name") or ""
    label = {
        "PHOTO": "[photo]",
        "VIDEO": "[video]",
        "VOICE": "[voice message]",
        "AUDIO": "[audio]",
        "STICKER": "[sticker]",
        "DOCUMENT": f"[document] {name}".strip(),
        "OTHER": "[media]",
    }.get(kind, "[media]")
    if len(records) > 1:
        label = f"{label} (+{len(records) - 1} files)"
    return label


# --------------------------------------------------------------------------- #
# Reaction helpers (CONTRACTS_V2 §4)
# --------------------------------------------------------------------------- #


def normalize_reaction_emoji(raw: Any) -> Optional[str]:
    """Map a raw reaction emoticon to one of :data:`SUPPORTED_REACTIONS`.

    Returns ``None`` for anything outside the six contract emoji. The usual
    suspects are handled explicitly:

    * custom emoji (``ReactionCustomEmoji``) never reach this function — the
      caller filters them — but a ``document_id`` passed by mistake does;
    * ``❤`` without ``U+FE0F`` is folded into ``❤️``;
    * skin-tone modifiers are stripped (``👍🏽`` counts as ``👍``).
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value in SUPPORTED_REACTIONS:
        return value
    # Emoji modifiers (skin tones) live in U+1F3FB..U+1F3FF.
    stripped = "".join(ch for ch in value if not ("\U0001F3FB" <= ch <= "\U0001F3FF"))
    stripped = stripped.replace("\u200d", "") if stripped not in SUPPORTED_REACTIONS else stripped
    if stripped in SUPPORTED_REACTIONS:
        return stripped
    return _REACTION_ALIASES.get(stripped)


def _peer_chat_id(peer: Any) -> Optional[int]:
    """Convert a ``TypePeer`` (channel/chat/user) to the marked ``-100…`` id.

    ``UpdateMessageReactions.peer`` is a bare ``PeerChannel``/``PeerChat``
    object, not a chat instance, so ``utils.get_peer_id`` is the only correct
    conversion: the backend stores the Bot-API style id and matches on it.
    """
    if peer is None:
        return None
    try:
        if isinstance(peer, PeerUser):
            user_id = to_int_or_none(getattr(peer, "user_id", None))
            return user_id
        if isinstance(peer, PeerChat):
            chat_id = to_int_or_none(getattr(peer, "chat_id", None))
            return -abs(chat_id) if chat_id is not None else None
    except Exception:  # pragma: no cover - defensive
        LOG.debug("peer id extraction failed for %r", peer, exc_info=True)
        return None
    channel_id = to_int_or_none(getattr(peer, "channel_id", None))
    if channel_id is not None:
        return -(1000000000000 + abs(channel_id))
    return to_int_or_none(peer)


def extract_reactions(reactions_payload: Any) -> list[dict[str, Any]]:
    """Normalise ``MessageReactions.results`` into the contract payload shape.

    Each element of the returned list is::

        {"emoji": "👍", "count": 2, "user_ids": [123]}

    ``user_ids`` lists the users Telegram attributes to this emoji via
    ``MessageReactions.recent_reactions``. Telegram reveals that per-user
    detail only for chats where the account may see who reacted; where it
    does not, ``user_ids`` stays empty and the backend works from ``count``.
    """
    results: list[dict[str, Any]] = []
    if reactions_payload is None:
        return results
    raw_results = getattr(reactions_payload, "results", None) or ()
    for result in raw_results:
        try:
            reaction = getattr(result, "reaction", None)
            if reaction is None:
                continue
            # Custom emoji (``ReactionCustomEmoji``) carry a numeric
            # ``document_id`` and are never one of the six contract emoji.
            if not isinstance(reaction, ReactionEmoji):
                continue
            emoji = normalize_reaction_emoji(getattr(reaction, "emoticon", None))
            if emoji is None:
                continue
            try:
                count = int(getattr(result, "count", 0) or 0)
            except (TypeError, ValueError):
                count = 0

            user_ids: list[int] = []
            recent = getattr(reactions_payload, "recent_reactions", None) or ()
            for peer_reaction in recent:
                try:
                    peer_emoji = normalize_reaction_emoji(
                        getattr(getattr(peer_reaction, "reaction", None), "emoticon", None)
                    )
                except Exception:  # pragma: no cover - defensive
                    continue
                if peer_emoji != emoji:
                    continue
                user_id = to_int_or_none(
                    getattr(getattr(peer_reaction, "peer_id", None), "user_id", None)
                )
                if user_id is not None and user_id not in user_ids:
                    user_ids.append(user_id)

            results.append({"emoji": emoji, "count": count, "user_ids": user_ids})
        except Exception:  # pragma: no cover - one bad entry must not lose the rest
            LOG.debug("skipping malformed reaction entry %r", result, exc_info=True)
    # Merge duplicates (Telegram never sends them, but a merge is cheap).
    merged: dict[str, dict[str, Any]] = {}
    for item in results:
        existing = merged.get(item["emoji"])
        if existing is None:
            merged[item["emoji"]] = item
            continue
        existing["count"] += item["count"]
        for user_id in item["user_ids"]:
            if user_id not in existing["user_ids"]:
                existing["user_ids"].append(user_id)
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Redis pub/sub wrapper
# --------------------------------------------------------------------------- #


@dataclass
class _PublishStats:
    """Lightweight counters exposed in ``worker:status`` for debugging."""

    incoming: int = 0
    deleted: int = 0
    edited: int = 0
    acks: int = 0
    failures: int = 0
    reactions: int = 0
    media_downloaded: int = 0
    media_failed: int = 0
    attachments_sent: int = 0
    #: CONTRACTS_V3 §1.3: successful ``tg_forward`` copies (``ok: true`` acks).
    forwarded: int = 0
    #: CONTRACTS_V5 §1.6: remote deletions done through ``tg_delete_out``. Messages
    #: that were already gone in Telegram count too — the goal was reached.
    delete_out: int = 0


class Bus:
    """Thin, failure-tolerant wrapper around ``redis.asyncio``.

    Every publish is wrapped: Redis hiccups are logged, never propagated into
    the Telethon event loop (a publish error must not kill the handler).
    """

    def __init__(self, redis_client: "aioredis.Redis") -> None:
        self._redis = redis_client
        self.stats = _PublishStats()

    @property
    def client(self) -> "aioredis.Redis":
        return self._redis

    async def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        """Publish one JSON payload; returns ``False`` when delivery failed."""
        try:
            await self._redis.publish(channel, _dumps(payload))
            return True
        except (RedisError, OSError) as exc:
            LOG.error("publish to %s failed: %s: %s | payload=%s",
                      channel, type(exc).__name__, exc, _safe_preview(payload))
            return False
        except Exception:
            LOG.exception("unexpected error publishing to %s | payload=%s",
                          channel, _safe_preview(payload))
            return False


def _safe_preview(payload: dict[str, Any], limit: int = 400) -> str:
    """Compact, log-safe rendering of a payload (text truncated)."""
    try:
        clone = {
            key: (value[:limit] + "…" if isinstance(value, str) and len(value) > limit else value)
            for key, value in payload.items()
        }
        return _dumps(clone)
    except Exception:
        return "<unserialisable payload>"


def _redis_endpoint_summary(redis_url: str) -> str:
    """Describe a Redis URL for logs **without** the password."""
    try:
        parsed = urllib.parse.urlsplit(redis_url)
    except Exception:
        return "<unparsable redis url>"
    host = parsed.hostname or "?"
    port = parsed.port or 6379
    db = (parsed.path or "/0").lstrip("/") or "0"
    auth = "auth=<set>" if (parsed.password or parsed.username) else "auth=<none>"
    scheme = parsed.scheme or "redis"
    return f"{scheme}://{host}:{port}/{db} {auth}"


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #


class TelethonWorker:
    """Supervised Telethon client bridged to Redis pub/sub.

    Lifecycle::

        worker = TelethonWorker(cfg)
        await worker.run()        # returns on KeyboardInterrupt / SIGTERM
    """

    def __init__(self, cfg: WorkerConfig) -> None:
        self.cfg = cfg
        self.client: Optional[TelegramClient] = None
        self.redis: Optional["aioredis.Redis"] = None
        self.bus: Optional[Bus] = None
        self._pubsub: Optional[Any] = None
        self._stop_event = asyncio.Event()
        self._started_at = time.monotonic()
        self._connected = False
        #: ``monitored_chat_ids`` JSON for ``worker:status``.
        self._monitored_json = _dumps(cfg.monitored_chat_ids)
        #: Resolved entity cache for outbound sends: chat id -> entity (LRU).
        self._entity_cache: OrderedDict[int, Any] = OrderedDict()
        self._entity_cache_max: int = 512
        #: Live session string created by the first-run login (in-memory only).
        self._runtime_session_string: str = ""
        #: Media store root (``storage/media`` by default) — CONTRACTS_V2 §5.
        self._media_root = cfg.media_root_path
        #: Ceiling for one attachment (bytes).
        self._max_download_bytes: int = max(1024, int(cfg.max_download_bytes))
        #: Bounds concurrent downloads so a photo album cannot starve the
        #: connection used for control messages (created lazily: a Semaphore
        #: binds to the running loop, and the worker survives reconnects).
        self._media_semaphore: Optional[asyncio.Semaphore] = None
        #: Duplicate guard for reactions: ``(chat_id, msg_id) -> fingerprint``.
        self._reaction_seen: dict[tuple[int, int], str] = {}
        #: True once the raw ``UpdateMessageReactions`` fallback was installed.
        self._raw_reactions_installed: bool = False
        #: Whether ``events.MessageReactionUpdated`` exists *and* was accepted.
        self._reaction_event_registered: bool = False
        #: True after the first reaction event of this run (disables the fallback).
        self._reaction_events_seen: bool = False
        #: Cached ``events.Raw`` callback so re-registration never duplicates it.
        self._raw_handler: Optional[Callable[[Any], Any]] = None
        #: Background task that installs the raw fallback when needed.
        self._watcher_task: Optional[asyncio.Task[Any]] = None

    # -- shutdown ---------------------------------------------------------- #

    def request_stop(self, reason: str) -> None:
        """Signal the supervisor to wind down (called by signals/Ctrl+C)."""
        if self._stop_event.is_set():
            return
        LOG.warning("stop requested: %s", reason)
        self._stop_event.set()

    @property
    def stopping(self) -> bool:
        """True once a shutdown has been requested."""
        return self._stop_event.is_set()

    # ------------------------------------------------------------------ #
    # Monitoring filter
    # ------------------------------------------------------------------ #

    def is_chat_monitored(
        self,
        tg_chat_id: Optional[int],
        title: Optional[str] = None,
        username: Optional[str] = None,
    ) -> bool:
        """True when this chat is in scope (empty config = every group).

        Numeric entries match ``tg_chat_id`` exactly; string entries match the
        username or title case-insensitively (substring of the title is
        accepted so ``"merchant alpha"`` matches
        ``"Merchant Alpha — Payouts"``).
        """
        wanted = self.cfg.monitored_chat_ids
        if not wanted:
            return True
        for entry in wanted:
            if isinstance(entry, bool):
                continue
            if isinstance(entry, int):
                if tg_chat_id is not None and entry == tg_chat_id:
                    return True
                continue
            needle = str(entry).strip().lstrip("@").lower()
            if not needle:
                continue
            if username and needle == username.lower():
                return True
            if title:
                haystack = title.lower()
                if needle == haystack or needle in haystack:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Client construction / handlers
    # ------------------------------------------------------------------ #

    def _ensure_session_file_usable(self) -> None:
        """Quarantine a corrupt/empty ``.session`` file so Telethon can recreate it.

        ``SQLiteSession`` assumes a valid schema: a truncated file, a 0-byte file or an
        interrupted write makes Telethon raise ``TypeError: 'NoneType' object is not
        subscriptable`` while reading the ``version`` table, which is fatal and gives no
        hint about the cause. Such a file can never hold a usable session, so it is moved
        aside (never deleted — it may still be needed for forensics) and a fresh login
        proceeds.
        """
        path = self.cfg.session_path
        if not path.exists():
            return
        try:
            import sqlite3

            rows: list[Any] = []
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                # A valid Telethon session always has a populated ``version`` row; the
                # table alone proves nothing (an interrupted first login leaves it empty).
                rows = conn.execute("SELECT version FROM version").fetchall()
            # ``with`` closes the connection; Windows refuses to rename an open file.
            if rows and rows[0][0] is not None:
                return
            problem = "empty 'version' table (interrupted login)" if rows else "no version row"
        except sqlite3.Error as exc:  # corrupt file, not a database, locked, ...
            problem = f"{type(exc).__name__}: {exc}"

        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            if backup.exists():
                backup.unlink()
            path.replace(backup)
            LOG.warning(
                "session file %s is unusable (%s); moved aside to %s and starting a fresh login",
                path.name,
                problem,
                backup.name,
            )
        except OSError as exc:
            LOG.error(
                "session file %s is unusable (%s) and could not be moved aside: %s; "
                "delete it manually before retrying",
                path.name,
                problem,
                exc,
            )

    def _build_client(self) -> TelegramClient:
        """Create a fresh ``TelegramClient`` (live string wins over file).

        Order: session created by the first-run login > ``TG_SESSION_STRING``
        > on-disk ``TG_SESSION_FILE``.
        """
        cfg = self.cfg
        live_string = self._runtime_session_string or cfg.session_string
        if live_string:
            session: Any = StringSession(live_string)
            session_kind = "StringSession"
        else:
            self._ensure_session_file_usable()
            session = str(cfg.session_path)
            session_kind = f"SQLiteSession({cfg.session_path.name})"

        client = TelegramClient(
            session,
            cfg.api_id,
            cfg.api_hash,
            connection_retries=None,  # retry forever; the supervisor also loops
            request_retries=5,
            auto_reconnect=True,
            device_model="TG-Desk CRM Worker",
            system_version="Windows 10",
            app_version="1.0.0",
        )
        LOG.info("Telegram client constructed (session=%s)", session_kind)
        return client

    def _register_handlers(self, client: TelegramClient) -> None:
        """Attach event handlers. MUST be called before ``connect()``."""
        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_message_deleted, events.MessageDeleted())
        client.add_event_handler(self._on_message_edited, events.MessageEdited())
        registered = ["NewMessage", "MessageDeleted", "MessageEdited"]

        # Reactions (CONTRACTS_V2 §4). Telethon 1.37.0 ships no
        # ``events.MessageReactionUpdated`` (reaction updates arrived in 1.38),
        # so the documented handler is registered when available and the raw
        # ``UpdateMessageReactions`` path is always attached as the
        # compatibility fallback. ``_on_raw_update`` de-duplicates the two
        # paths, so registering both is safe on 1.38+.
        reaction_event = getattr(events, "MessageReactionUpdated", None)
        self._reaction_event_registered = reaction_event is not None
        if reaction_event is not None:
            try:
                client.add_event_handler(self._on_reaction_event, reaction_event())
                registered.append("MessageReactionUpdated")
            except Exception:
                self._reaction_event_registered = False
                LOG.warning("could not register MessageReactionUpdated; "
                            "falling back to raw UpdateMessageReactions", exc_info=True)
        if self._register_raw_reactions_handler(client):
            registered.append("Raw(UpdateMessageReactions)")
        elif not self._reaction_event_registered:
            LOG.error(
                "no reaction handler could be registered — reactions from "
                "Telegram will NOT reach the CRM (check the Telethon version)"
            )
        LOG.debug("handlers registered: %s", ", ".join(registered))
        self._watcher_task = asyncio.ensure_future(self._watch_reaction_handlers(client))
        LOG.debug("reaction handler watcher started")

    def _register_raw_reactions_handler(self, client: TelegramClient) -> bool:
        """Attach the raw ``UpdateMessageReactions`` handler once. Returns success."""
        try:
            handler = getattr(self, "_raw_handler", None)
            if handler is None:
                handler = self._make_raw_handler()
            client.add_event_handler(handler, events.Raw(types=(UpdateMessageReactions,)))
            self._raw_reactions_installed = True
            return True
        except Exception:
            LOG.warning("could not register the raw reaction handler", exc_info=True)
            return False

    def _make_raw_handler(self) -> Any:
        """Build (and cache) an ``events.Raw`` callback for reactions.

        ``events.Raw`` passes the ``Update`` object itself (not an event), so
        the handler is a dedicated coroutine rather than the event-style one.
        """

        async def _raw_reaction_handler(update: Any) -> None:
            await self._on_raw_update(update)

        _raw_reaction_handler.__name__ = "_raw_reaction_handler"
        self._raw_handler = _raw_reaction_handler
        return _raw_reaction_handler

    async def _watch_reaction_handlers(self, client: TelegramClient) -> None:
        """Install the raw fallback if ``MessageReactionUpdated`` stopped firing.

        Telethon's dispatcher silently drops every builder whose ``build()``
        returns ``None`` for the update — exactly what happens when a client
        build advertises ``MessageReactionUpdated`` but the installed version
        cannot build it. After two full minutes without a single reaction
        event the worker logs a warning and adds the raw handler, which always
        works because it receives the ``Update`` object untouched.
        """
        try:
            await asyncio.sleep(120)
            if self._raw_reactions_installed:
                return
            if not self._reaction_event_registered or self._reaction_events_seen:
                return
            LOG.warning(
                "no MessageReactionUpdated received in 2 minutes — installing the "
                "raw UpdateMessageReactions fallback for this Telethon build"
            )
            if self._register_raw_reactions_handler(client):
                LOG.info("raw reaction fallback installed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug("reaction handler watcher failed", exc_info=True)

    def _new_client(self) -> TelegramClient:
        """Build + wire a client (handlers registered before connect)."""
        client = self._build_client()
        self._register_handlers(client)
        return client

    # ------------------------------------------------------------------ #
    # Handlers (all exceptions contained — the loop must never die)
    # ------------------------------------------------------------------ #

    async def _on_new_message(self, event: "events.NewMessage.Event") -> None:
        """``events.NewMessage`` handler -> publish ``NEW_MESSAGE``.

        Rules:
          * messages sent by **our own account** (``out=True``) are ignored —
            CRM replies are already stored by the backend when they are queued
            on ``tg_outgoing``; re-ingesting the echo would double-post;
          * private chats and broadcast channels are ignored (groups and
            supergroups only);
          * ``cfg.monitored_chat_ids`` is honoured when non-empty;
          * media-only messages are forwarded with a ``"[photo]"``-style
            placeholder instead of an empty body.

        Exceptions are logged with a traceback and swallowed.
        """
        try:
            await self._handle_new_message(event)
        except Exception:
            LOG.exception("NEW_MESSAGE handler crashed (message ignored)")

    async def _handle_new_message(self, event: "events.NewMessage.Event") -> None:
        message = event.message
        if message is None:
            return

        # 1) Echo / own-message filtering -------------------------------- #
        if getattr(message, "out", False):
            LOG.debug("skip outbound echo chat=%s msg=%s", event.chat_id, getattr(message, "id", "?"))
            return

        # 2) Chat type filtering ---------------------------------------- #
        if getattr(event, "is_private", False):
            return
        if getattr(event, "is_channel", False) and not getattr(event, "is_group", False):
            return

        tg_chat_id = peer_id_of(event)
        if tg_chat_id is None:
            LOG.warning("dropping message with unresolvable chat id (msg=%s)", getattr(message, "id", "?"))
            return

        # 3) Chat entity / merchant code --------------------------------- #
        chat = event.chat
        chat_title: str = ""
        if chat is not None:
            chat_title = getattr(chat, "title", None) or ""
        if not chat_title:
            chat_title = str(tg_chat_id)
        chat_username: Optional[str] = getattr(chat, "username", None) or None

        if not self.is_chat_monitored(tg_chat_id, chat_title, chat_username):
            LOG.debug("chat %s (%s) not in TG_MONITORED_CHAT_IDS — skipped", tg_chat_id, chat_title)
            return

        try:
            merchant_code = derive_merchant_code(title=chat_title, chat_username=chat_username)
        except Exception:
            LOG.exception("merchant code derivation failed for %s", chat_title)
            merchant_code = None

        # 4) Sender -------------------------------------------------------- #
        sender: Any = None
        try:
            sender = await event.get_sender()
        except Exception:
            LOG.warning("get_sender() failed for chat=%s msg=%s",
                        tg_chat_id, getattr(message, "id", "?"), exc_info=True)

        sender_name = "Unknown"
        sender_username: Optional[str] = None
        sender_tg_id: Optional[int] = None
        is_bot = False
        if sender is not None:
            first = getattr(sender, "first_name", None) or ""
            last = getattr(sender, "last_name", None) or ""
            full = f"{first} {last}".strip()
            sender_name = full or getattr(sender, "title", None) or "Unknown"
            sender_username = getattr(sender, "username", None) or None
            is_bot = bool(getattr(sender, "bot", False))
            sender_tg_id = to_int_or_none(getattr(sender, "id", None))
        else:
            sender_tg_id = to_int_or_none(getattr(message, "sender_id", None))

        # 5) Body ---------------------------------------------------------- #
        text = getattr(event, "raw_text", None) or ""
        kind = message_kind(message)

        # 5a) Media (CONTRACTS_V2 §4/§5): download first so a caption-less
        #     photo still gets a meaningful body ("[photo]"), then publish one
        #     payload carrying both the text and the attachment list.
        media_records: list[dict[str, Any]] = []
        if getattr(message, "media", None) is not None:
            media_records = await self._download_attachments(message, tg_chat_id)

        caption = _caption_text(message, text)
        if not text:
            text = _attachment_placeholder(media_records) or message_media_placeholder(message)
            if not text:
                # Service-ish or empty message: still forward so the backend can
                # keep its cursor / unread counter honest.
                text = "[empty message]"

        # Defence in depth: drop our own CRM echo if Telegram reported it as in.
        if consume_pending_send(tg_chat_id, text):
            LOG.debug("dropping CRM echo chat=%s msg=%s", tg_chat_id, getattr(message, "id", "?"))
            return

        # 6) Reply target (forums nest it one level deeper) ---------------- #
        reply_to_tg_msg_id: Optional[int] = to_int_or_none(getattr(event, "reply_to_msg_id", None))
        if reply_to_tg_msg_id is None:
            try:
                reply_to_msg_id = getattr(message, "reply_to_msg_id", None)
                nested = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
                reply_to_tg_msg_id = to_int_or_none(reply_to_msg_id or nested)
            except Exception:
                reply_to_tg_msg_id = None

        tg_date = utc_iso(getattr(message, "date", None))

        payload: dict[str, Any] = {
            "kind": KIND_NEW_MESSAGE,
            "tg_chat_id": tg_chat_id,
            "tg_msg_id": to_int_or_none(getattr(message, "id", None)),
            "chat_title": chat_title,
            "chat_username": chat_username,
            "sender_tg_id": sender_tg_id,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "is_bot": is_bot,
            # ``sender_type`` is the CONTRACT field (bots must never open SLA);
            # ``is_bot`` is kept for the older backend revisions.
            "sender_type": "BOT" if is_bot else "USER",
            "text": text,
            # CONTRACTS_V2 §2.4: ``MessageKind`` of this message. The envelope
            # discriminator is the ``kind`` key above ("NEW_MESSAGE"), so the
            # message kind travels as ``message_kind``.
            "message_kind": kind,
            # CONTRACTS_V2 §4: caption of a media message (None for pure text).
            "caption": caption,
            "attachment_count": len(media_records),
            # CONTRACTS_V2 §4: one dict per downloaded attachment (empty list
            # when the message has no media).
            "media": media_records,
            "reply_to_tg_msg_id": reply_to_tg_msg_id,
            "tg_date": tg_date,
            "merchant_code": merchant_code,
        }

        bus = self.bus
        if bus is None:
            LOG.error("Redis bus unavailable — NEW_MESSAGE dropped (chat=%s msg=%s)",
                      tg_chat_id, payload["tg_msg_id"])
            return

        if await bus.publish(CHANNEL_INCOMING, payload):
            bus.stats.incoming += 1
            LOG.info("NEW_MESSAGE chat=%s msg=%s from=%s bot=%s kind=%s media=%d len=%d code=%s",
                     tg_chat_id, payload["tg_msg_id"], sender_name, is_bot, kind,
                     len(media_records), len(text), merchant_code)

    async def _download_attachments(
        self, message: Any, tg_chat_id: int
    ) -> list[dict[str, Any]]:
        """Download every attachment of ``message`` (CONTRACTS_V2 §5).

        A single Telegram message carries at most one ``media`` object, but the
        contract speaks of a *list*: the album case, where the same logical
        message is delivered as several, is collapsed by the backend, and a
        future carousel support needs no payload change. Returning a list here
        keeps both consumers happy.

        Errors are contained: a failed download logs a warning and yields an
        empty list, so the text of the message is still published.
        """
        client = self.client
        if client is None:
            LOG.warning("media skipped (chat=%s msg=%s): telegram client is not ready",
                        tg_chat_id, getattr(message, "id", "?"))
            return []

        if self._media_semaphore is None:
            self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_DOWNLOADS)

        try:
            async with self._media_semaphore:
                record = await asyncio.wait_for(
                    media_store.download_media(
                        client,
                        message,
                        self._media_root,
                        tg_chat_id,
                        max_download_bytes=self._max_download_bytes,
                    ),
                    timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            LOG.error("media download timed out after %ss (chat=%s msg=%s)",
                      MEDIA_DOWNLOAD_TIMEOUT_SECONDS, tg_chat_id, getattr(message, "id", "?"))
            if self.bus is not None:
                self.bus.stats.media_failed += 1
            return []
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("media download crashed (chat=%s msg=%s) — message kept",
                          tg_chat_id, getattr(message, "id", "?"))
            if self.bus is not None:
                self.bus.stats.media_failed += 1
            return []

        if record is None:
            return []
        if self.bus is not None:
            if record.get("storage_path"):
                self.bus.stats.media_downloaded += 1
            elif record.get("size_bytes"):
                # Oversized by contract: not an error, but worth counting.
                self.bus.stats.media_failed += 1
        return [record]

    async def _on_message_deleted(self, event: "events.MessageDeleted.Event") -> None:
        """``events.MessageDeleted`` handler -> publish ``MESSAGE_DELETED``.

        Payload (CONTRACTS §5)::

            {"kind": "MESSAGE_DELETED", "tg_chat_id": -100…, "tg_msg_ids": [1, 2]}

        ``tg_chat_id`` is **nullable on purpose**: Telegram sometimes only
        reports "$N messages were deleted" without saying where (this happens
        for events older than the current session, after reconnecting, and for
        moderator deletions in supergroups). The backend handles ``null`` by
        matching the message ids **globally** across its ``group_messages``
        table, so the UI still flips them to ``is_deleted_in_tg``. We must
        forward the ids even when the chat is unknown — dropping them would
        leave deleted messages visible in the CRM forever.
        """
        try:
            deleted_ids: list[int] = []
            for raw_id in getattr(event, "deleted_ids", None) or ():
                as_int = to_int_or_none(raw_id)
                if as_int is not None:
                    deleted_ids.append(as_int)
            if not deleted_ids:
                return

            tg_chat_id = to_int_or_none(getattr(event, "chat_id", None))
            payload: dict[str, Any] = {
                "kind": KIND_MESSAGE_DELETED,
                "tg_chat_id": tg_chat_id,          # may be None — see docstring
                "tg_msg_ids": deleted_ids,
            }

            bus = self.bus
            if bus is None:
                LOG.error("Redis bus unavailable — MESSAGE_DELETED dropped ids=%s", deleted_ids)
                return
            if await bus.publish(CHANNEL_DELETED, payload):
                bus.stats.deleted += 1
                LOG.info("MESSAGE_DELETED chat=%s ids=%s", tg_chat_id, deleted_ids)
        except Exception:
            LOG.exception("MESSAGE_DELETED handler crashed (event ignored)")

    async def _on_message_edited(self, event: "events.MessageEdited.Event") -> None:
        """``events.MessageEdited`` handler -> publish ``MESSAGE_EDITED``.

        Payload (CONTRACTS §5, extended by CONTRACTS_V3 §2.5)::

            {"kind": "MESSAGE_EDITED", "tg_chat_id": -100…, "tg_msg_id": 90210,
             "text": "…", "edited_at": "2024-06-01T12:00:00+00:00",
             "actor_name": "Jane Doe", "actor_username": "jane"}

        ``edited_at`` is additive (the contract's minimum is the first three
        fields) and carries ``message.edit_date``, which is what actually
        changed for the backend's audit trail.

        ``actor_*`` feed ``message_edits.actor_name/actor_username``: a client
        editing their own wording must be attributable. Both degrade to
        ``None`` instead of dropping the edit — the text version matters more
        than the name of who changed it.
        """
        try:
            message = getattr(event, "message", None)
            if message is None:
                return
            tg_chat_id = peer_id_of(event)
            if tg_chat_id is None:
                LOG.warning("MESSAGE_EDITED without chat id (msg=%s) — skipped",
                            getattr(message, "id", "?"))
                return

            text = getattr(event, "raw_text", None) or ""
            if not text:
                text = message_media_placeholder(message) or ""

            actor_name, actor_username = await self._describe_sender(
                event, tg_chat_id, getattr(message, "id", "?")
            )

            payload: dict[str, Any] = {
                "kind": KIND_MESSAGE_EDITED,
                "tg_chat_id": tg_chat_id,
                "tg_msg_id": to_int_or_none(getattr(message, "id", None)),
                "text": text,
                "edited_at": utc_iso(getattr(message, "edit_date", None)),
                "actor_name": actor_name,
                "actor_username": actor_username,
            }

            bus = self.bus
            if bus is None:
                LOG.error("Redis bus unavailable — MESSAGE_EDITED dropped")
                return
            if await bus.publish(CHANNEL_EDITS, payload):
                bus.stats.edited += 1
                LOG.info("MESSAGE_EDITED chat=%s msg=%s len=%d actor=%s",
                         tg_chat_id, payload["tg_msg_id"], len(text),
                         actor_name or actor_username or "?")
        except Exception:
            LOG.exception("MESSAGE_EDITED handler crashed (event ignored)")

    async def _describe_sender(
        self,
        event: Any,
        tg_chat_id: Optional[int],
        tg_msg_id: Any,
    ) -> tuple[Optional[str], Optional[str]]:
        """Return ``(actor_name, actor_username)`` for an event, never raises.

        Name precedence follows CONTRACTS_V3 §2.5: first+last name, then the
        entity ``title`` (channels/bots), then the username, then ``None``.
        An unresolvable sender (deleted account, stripped access hash, a raw
        update without a peer) yields ``(None, None)``.
        """
        try:
            sender = await event.get_sender()
        except Exception as exc:
            LOG.warning("get_sender() failed for edited msg chat=%s msg=%s (%s: %s)",
                        tg_chat_id, tg_msg_id, type(exc).__name__, exc)
            return None, None
        if sender is None:
            return None, None
        try:
            first = getattr(sender, "first_name", None) or ""
            last = getattr(sender, "last_name", None) or ""
            name = f"{first} {last}".strip() or getattr(sender, "title", None) or None
            username = getattr(sender, "username", None) or None
            if not name and username:
                name = username
            return name, username
        except Exception:  # pragma: no cover - defensive, exotic entity proxies
            LOG.debug("could not render sender for chat=%s msg=%s",
                      tg_chat_id, tg_msg_id, exc_info=True)
            return None, None

    # ------------------------------------------------------------------ #
    # Reaction handlers (CONTRACTS_V2 §4)
    # ------------------------------------------------------------------ #

    async def _on_reaction_event(self, event: Any) -> None:
        """``events.MessageReactionUpdated`` handler (Telethon >= 1.38).

        The event exposes ``chat_id``/``message_id``/``new_reactions``
        (``MessageReactions``, or ``None`` when every reaction was removed).
        Exceptions are swallowed exactly like in the other handlers.
        """
        try:
            self._reaction_events_seen = True
            tg_chat_id = to_int_or_none(getattr(event, "chat_id", None))
            tg_msg_id = to_int_or_none(
                getattr(event, "message_id", None) or getattr(event, "msg_id", None)
            )
            await self._publish_reaction_update(
                tg_chat_id, tg_msg_id, getattr(event, "new_reactions", None)
            )
        except Exception:
            LOG.exception("MessageReactionUpdated handler crashed (update ignored)")

    async def _on_raw_update(self, update: Any) -> None:
        """Raw ``UpdateMessageReactions`` fallback (works on Telethon 1.37).

        ``events.Raw`` hands us the ``Update`` object; its ``peer`` is a bare
        ``PeerChannel``/``PeerChat``/``PeerUser`` instance. If the
        ``MessageReactionUpdated`` builder is also installed both callbacks
        fire for one update — :meth:`_publish_reaction_update` de-duplicates.
        """
        try:
            if not isinstance(update, UpdateMessageReactions):
                return
            tg_chat_id = _peer_chat_id(getattr(update, "peer", None))
            tg_msg_id = to_int_or_none(getattr(update, "msg_id", None))
            await self._publish_reaction_update(
                tg_chat_id, tg_msg_id, getattr(update, "reactions", None)
            )
        except Exception:
            LOG.exception("raw UpdateMessageReactions handler crashed (update ignored)")

    async def _publish_reaction_update(
        self, tg_chat_id: Optional[int], tg_msg_id: Optional[int], reactions_payload: Any
    ) -> None:
        """Publish one ``REACTION_UPDATE`` envelope to ``tg_reactions``.

        Payload (CONTRACTS_V2 §4)::

            {"kind": "REACTION_UPDATE", "tg_chat_id": -100…, "tg_msg_id": 93,
             "reactions": [{"emoji": "👍", "count": 2, "user_ids": [123]}]}

        An empty ``reactions`` list means "all reactions removed" — the
        backend uses it to delete its rows, so the envelope is published
        rather than skipped.
        """
        if tg_msg_id is None:
            LOG.debug("reaction update without message id — skipped")
            return
        if tg_chat_id is None:
            LOG.warning("reaction update without resolvable chat (msg=%s) — skipped", tg_msg_id)
            return
        if not self.is_chat_monitored(tg_chat_id):
            LOG.debug("reaction update for unmonitored chat %s — skipped", tg_chat_id)
            return

        reactions = extract_reactions(reactions_payload)
        fingerprint = _dumps(reactions)
        key = (tg_chat_id, tg_msg_id)
        if self._reaction_seen.get(key) == fingerprint:
            # Duplicate delivery (both handlers fired on 1.38+, or Telegram
            # resent the update after a reconnect): republishing would
            # double-count on the backend.
            LOG.debug("duplicate reaction update chat=%s msg=%s — skipped",
                      tg_chat_id, tg_msg_id)
            return
        if len(self._reaction_seen) > 5000:
            self._reaction_seen.clear()
        self._reaction_seen[key] = fingerprint

        payload: dict[str, Any] = {
            "kind": KIND_REACTION_UPDATE,
            "tg_chat_id": tg_chat_id,
            "tg_msg_id": tg_msg_id,
            "reactions": reactions,
        }

        bus = self.bus
        if bus is None:
            LOG.error("Redis bus unavailable — REACTION_UPDATE dropped (chat=%s msg=%s)",
                      tg_chat_id, tg_msg_id)
            return
        if await bus.publish(CHANNEL_REACTIONS, payload):
            bus.stats.reactions += 1
            LOG.info("REACTION_UPDATE chat=%s msg=%s reactions=%s", tg_chat_id, tg_msg_id,
                     ", ".join(f"{item['emoji']}x{item['count']}" for item in reactions)
                     or "<cleared>")

    # ------------------------------------------------------------------ #
    # Outgoing loop (backend -> worker)
    # ------------------------------------------------------------------ #

    async def outgoing_loop(self) -> None:
        """Consume ``tg_outgoing``, ``tg_forward``, ``tg_reaction_out`` and ``tg_delete_out``.

        Every command is acked exactly once on ``tg_outgoing_ack`` — even when
        it fails — because the backend flips the row to ``failed`` only on
        ``ok: false`` and would otherwise leave it ``pending`` forever.

        CONTRACTS_V3 §1.3 adds the ``tg_forward`` channel to the same
        subscription: both channels carry a ``cmd_id``/``message_id`` command
        and both are answered on ``tg_outgoing_ack``, so one pubsub and one
        dispatcher (:meth:`_pump_outgoing`) keep the ordering guarantees.
        CONTRACTS_V4 §2 (``tg_reaction_out``) and CONTRACTS_V5 §1.4
        (``tg_delete_out``) follow exactly the same pattern.

        The listener is raced against the stop event: ``pubsub.listen()`` is
        an infinite async generator that only yields on a new message, so
        awaiting it directly would block shutdown until the backend happened
        to publish something.
        """
        redis_client = self.redis
        if redis_client is None:
            raise RuntimeError("outgoing_loop started before Redis was initialised")

        while not self.stopping:
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            self._pubsub = pubsub
            listener: Optional[asyncio.Task[Any]] = None
            try:
                await pubsub.subscribe(
                    CHANNEL_OUTGOING, CHANNEL_FORWARD, CHANNEL_REACTION_OUT,
                    CHANNEL_DELETE_OUT,
                )
                LOG.info("subscribed to %s, %s, %s, %s",
                         CHANNEL_OUTGOING, CHANNEL_FORWARD, CHANNEL_REACTION_OUT,
                         CHANNEL_DELETE_OUT)
                listener = asyncio.create_task(self._pump_outgoing(pubsub), name="outgoing-pump")
                stop_waiter = asyncio.create_task(self._stop_event.wait(), name="stop-waiter-pubsub")
                done, _pending = await asyncio.wait(
                    {listener, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
                if listener in done:
                    exc = listener.exception()
                    if exc is not None:
                        raise exc
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                LOG.error("outgoing pub/sub lost connection: %s: %s", type(exc).__name__, exc)
            except Exception:
                LOG.exception("outgoing loop error; resubscribing in 3s")
            finally:
                if listener is not None and not listener.done():
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
                self._pubsub = None
                try:
                    await pubsub.aclose()  # redis>=5 (`close()` on 4.x)
                except AttributeError:  # pragma: no cover - redis 4.x
                    try:
                        await pubsub.close()  # type: ignore[attr-defined]
                    except Exception:
                        LOG.debug("pubsub close failed", exc_info=True)
                except Exception:
                    LOG.debug("pubsub close failed", exc_info=True)

            if self.stopping:
                break
            await asyncio.sleep(3.0)

        LOG.info("outgoing loop stopped")

    async def _pump_outgoing(self, pubsub: Any) -> None:
        """Dispatch every pub/sub message to the handler owning its channel.

        One subscription serves two channels (CONTRACTS_V3 §1.3), and
        ``pubsub.listen()`` does not tell the two apart for us. The routing
        therefore happens **here**, so :meth:`_handle_outgoing_message` keeps
        its legacy "parse then send" contract untouched and a ``tg_forward``
        payload can never be mistaken for a plain ``tg_outgoing`` send (which
        would ignore ``target_tg_chat_id`` and drop ``attachment_path``).

        A message without a recognised ``channel`` keeps the historical
        behaviour and is treated as ``tg_outgoing``.

        CONTRACTS_V5 §1.4 adds ``tg_delete_out`` to the same fan-in: a deletion
        is a command with a ``cmd_id`` and is acked on ``tg_outgoing_ack``
        exactly like a send or a reaction, so it reuses the same pump (and, as a
        consequence, the same FIFO ordering — a delete published after a send
        can never overtake it).
        """
        async for message in pubsub.listen():
            if self.stopping:
                return
            try:
                channel = _pubsub_channel(message)
                if channel == CHANNEL_FORWARD:
                    await self._handle_forward_command(message)
                elif channel == CHANNEL_REACTION_OUT:
                    await self._handle_reaction_command(message)
                elif channel == CHANNEL_DELETE_OUT:
                    await self._handle_delete_command(message)
                else:
                    await self._handle_outgoing_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("outgoing command handling crashed (skipped)")

    async def _handle_outgoing_message(self, message: dict[str, Any]) -> None:
        """Parse & execute one ``tg_outgoing`` pub/sub message."""
        if message.get("type") != "message":
            return
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            LOG.error("tg_outgoing payload is not text (type=%s)", type(data).__name__)
            return

        try:
            command = json.loads(data)
        except json.JSONDecodeError as exc:
            LOG.error("tg_outgoing payload is not valid JSON (%s): %s", exc, data[:300])
            return
        if not isinstance(command, dict):
            LOG.error("tg_outgoing payload must be a JSON object, got %s", type(command).__name__)
            return

        cmd_id = str(command.get("cmd_id") or "") or None
        chat_id = to_int_or_none(command.get("chat_id"))
        tg_chat_id = to_int_or_none(command.get("tg_chat_id"))
        message_id = to_int_or_none(command.get("message_id"))
        text = command.get("text")
        reply_to_tg_msg_id = to_int_or_none(command.get("reply_to_tg_msg_id"))
        raw_attachment = command.get("attachment_path")
        # CONTRACTS_V3 §2.4: optional field — when present the command edits an
        # existing Telegram message instead of sending a new one.
        edit_tg_msg_id = to_int_or_none(command.get("edit_tg_msg_id"))

        # CONTRACTS_V2 §5: a command may carry an absolute path prepared by the
        # backend. An empty/blank value means "plain text message" and keeps
        # the legacy ``send_message`` path untouched.
        attachment_path: Optional[Path] = None
        if raw_attachment is not None and str(raw_attachment).strip():
            attachment_path = Path(str(raw_attachment).strip().strip("\"'"))
            if not attachment_path.is_absolute():
                await self._ack(
                    cmd_id, chat_id, message_id, None, False,
                    f"ValueError: attachment_path must be absolute ({attachment_path})",
                )
                return
            if not attachment_path.is_file():
                LOG.error("attachment missing on disk: %s (cmd=%s)", attachment_path, cmd_id)
                await self._ack(
                    cmd_id, chat_id, message_id, None, False,
                    f"FileNotFoundError: attachment_path does not exist ({attachment_path.name})",
                )
                return

        caption = text if isinstance(text, str) else ""
        if edit_tg_msg_id is not None:
            # CONTRACTS_V3 §2.4: an edit is validated by ``_edit_outgoing``
            # (its "empty text" error is a contract value, not the legacy send
            # error), so the send-specific body/caption rules are skipped.
            LOG.debug("cmd=%s is an edit of msg=%s — skipping send validation",
                      cmd_id, edit_tg_msg_id)
        elif attachment_path is not None:
            # A caption is optional; the text becomes the file's caption and
            # the 1024-character caption limit applies instead of 4096.
            if caption and len(caption) > TG_MAX_FILE_CAPTION_LENGTH:
                await self._ack(
                    cmd_id, chat_id, message_id, None, False,
                    f"ValueError: caption too long ({len(caption)} > "
                    f"{TG_MAX_FILE_CAPTION_LENGTH} chars for a file)",
                )
                return
        else:
            if not isinstance(text, str) or not text.strip():
                await self._ack(cmd_id, chat_id, message_id, None, False,
                                "ValueError: empty text")
                return

            if len(text) > TG_MAX_MESSAGE_LENGTH:
                await self._ack(
                    cmd_id, chat_id, message_id, None, False,
                    f"ValueError: text too long ({len(text)} > {TG_MAX_MESSAGE_LENGTH} chars)",
                )
                return

        target = tg_chat_id
        if target is None and edit_tg_msg_id is not None and chat_id is not None:
            # An edit does not create a message, so falling back to the CRM's
            # own chat id (as ``tg_forward`` does) is meaningful here: an
            # arbitrary numeric id can only fail to resolve, never mis-send.
            LOG.warning("tg_outgoing edit cmd=%s has no tg_chat_id; falling back "
                        "to chat_id=%s as a Telegram id", cmd_id, chat_id)
            target = chat_id
        if target is None:
            # ``chat_id`` is the CRM's own integer id (group_chats.id) and is
            # meaningless to Telegram: we must not guess. The backend needs a
            # negative ack to mark the row `failed` instead of hanging in
            # `pending`.
            LOG.error(
                "tg_outgoing cmd=%s chat_id=%s has no tg_chat_id — cannot send "
                "(backend must always include telegram id)", cmd_id, chat_id,
            )
            await self._ack(
                cmd_id, chat_id, message_id, None, False,
                "ValueError: tg_chat_id is required to send a Telegram message",
            )
            return

        client = self.client
        if client is None or not self._connected:
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            "ConnectionError: telegram client is not connected")
            return

        # CONTRACTS_V3 §2.4: an edit is a wholly different operation — reply
        # targets and attachments are meaningless for it and are ignored.
        if edit_tg_msg_id is not None:
            await self._edit_outgoing(
                client, cmd_id, chat_id, message_id, edit_tg_msg_id, text, target,
            )
            return

        sender_entity = await self._resolve_entity(client, target)

        try:
            if attachment_path is not None:
                sent = await asyncio.wait_for(
                    self._send_attachment(
                        client, sender_entity, attachment_path,
                        caption=caption, reply_to=reply_to_tg_msg_id or None,
                    ),
                    timeout=SEND_TIMEOUT_SECONDS,
                )
            else:
                sent = await asyncio.wait_for(
                    client.send_message(
                        entity=sender_entity,
                        message=text,
                        reply_to=reply_to_tg_msg_id or None,
                        parse_mode=None,
                        link_preview=False,
                    ),
                    timeout=SEND_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            LOG.error("send timed out after %ss (cmd=%s chat=%s)",
                      SEND_TIMEOUT_SECONDS, cmd_id, target)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"TimeoutError: send exceeded {SEND_TIMEOUT_SECONDS}s")
            return
        except FloodWaitError as exc:
            wait_seconds = min(int(getattr(exc, "seconds", 0) or 0), FLOOD_WAIT_CAP_SECONDS)
            LOG.warning("FloodWaitError on chat=%s: telegram asks for %ss (capping at %ss)",
                        target, getattr(exc, "seconds", "?"), wait_seconds)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"FloodWaitError: {exc}")
            return
        except RPCError as exc:
            LOG.error("RPCError sending to chat=%s (cmd=%s): %s: %s",
                      target, cmd_id, type(exc).__name__, exc)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"{type(exc).__name__}: {exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("unexpected error sending to chat=%s (cmd=%s)", target, cmd_id)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"{type(exc).__name__}: {exc}")
            return

        sent_id = to_int_or_none(getattr(sent, "id", None))
        # Suppress the Telegram echo of our own message (30 s window). Files
        # echo back with their caption (or nothing at all), so only a caption
        # can be matched — a caption-less file relies on the hero ``out`` flag.
        if attachment_path is None:
            register_pending_send(target, text)
            if chat_id is not None:
                register_pending_send(chat_id, text)
        elif caption:
            register_pending_send(target, caption)
            if chat_id is not None:
                register_pending_send(chat_id, caption)

        attachment_sent = attachment_path is not None
        if attachment_sent and self.bus is not None:
            self.bus.stats.attachments_sent += 1
        LOG.info("SENT cmd=%s chat=%s(crm=%s) tg_msg_id=%s attachment=%s",
                 cmd_id, target, chat_id, sent_id,
                 attachment_path.name if attachment_path is not None else "-")
        await self._ack(cmd_id, chat_id, message_id, sent_id, True, None,
                        attachment_sent=attachment_sent)

    async def _edit_outgoing(
        self,
        client: TelegramClient,
        cmd_id: Optional[str],
        chat_id: Optional[int],
        message_id: Optional[int],
        edit_tg_msg_id: int,
        text: Any,
        target: int,
    ) -> None:
        """Edit an existing Telegram message (CONTRACTS_V3 §2.4).

        Rules straight from the contract:

        * empty/blank text is refused **before** touching Telegram — the
          Bot/Client API rejects empty bodies, so the CRM must not wait for an
          RPC error to learn that;
        * ``MessageNotModifiedError`` means the message already says exactly
          this — the edit succeeded, so the ACK stays ``ok: true`` with the id
          of the message that was edited (it must not be reported as failed,
          which would leave the row dirty in the CRM);
        * the edited message id is echoed back as ``tg_msg_id``.
        """
        if not isinstance(text, str) or not text.strip():
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            "empty text")
            return
        if len(text) > TG_MAX_MESSAGE_LENGTH:
            await self._ack(
                cmd_id, chat_id, message_id, None, False,
                f"ValueError: text too long ({len(text)} > {TG_MAX_MESSAGE_LENGTH} chars)",
            )
            return

        try:
            entity = await self._resolve_entity(client, target)
            edited = await asyncio.wait_for(
                client.edit_message(
                    entity=entity,
                    message=edit_tg_msg_id,
                    text=text,
                    parse_mode=None,
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOG.error("edit timed out after %ss (cmd=%s chat=%s msg=%s)",
                      SEND_TIMEOUT_SECONDS, cmd_id, target, edit_tg_msg_id)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"TimeoutError: edit exceeded {SEND_TIMEOUT_SECONDS}s")
            return
        except MessageNotModifiedError:
            # Telegram refuses a no-op edit; the desired state is already true.
            LOG.info("EDIT cmd=%s chat=%s msg=%s: text unchanged (MessageNotModified)",
                     cmd_id, target, edit_tg_msg_id)
            await self._ack(cmd_id, chat_id, message_id, edit_tg_msg_id, True, None)
            return
        except FloodWaitError as exc:
            wait_seconds = min(int(getattr(exc, "seconds", 0) or 0), FLOOD_WAIT_CAP_SECONDS)
            LOG.warning("FloodWaitError on edit in chat=%s: telegram asks for %ss "
                        "(capping at %ss)", target, getattr(exc, "seconds", "?"), wait_seconds)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"FloodWaitError: {exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("edit failed chat=%s msg=%s (cmd=%s): %s: %s",
                      target, edit_tg_msg_id, cmd_id, type(exc).__name__, exc)
            await self._ack(cmd_id, chat_id, message_id, None, False,
                            f"{type(exc).__name__}: {exc}")
            return

        edited_id = to_int_or_none(getattr(edited, "id", None)) or edit_tg_msg_id
        LOG.info("EDIT cmd=%s chat=%s msg=%s tg_msg_id=%s", cmd_id, target,
                 edit_tg_msg_id, edited_id)
        await self._ack(cmd_id, chat_id, message_id, edited_id, True, None)

    # ------------------------------------------------------------------ #
    # Forwarding (CONTRACTS_V3 §1.3) — always a plain re-send, never a
    # Telegram forward
    # ------------------------------------------------------------------ #

    async def _handle_forward_command(self, message: dict[str, Any]) -> None:
        """Parse & execute one ``tg_forward`` pub/sub message.

        CONTRACTS_V3 §1.3 / §1.1 — the operator's copy must look like an
        ordinary message from **our** account: no ``forward_messages``, no
        ``ForwardMessagesRequest``, no ``MessageFwdHeader``. Only the text (or
        the caption) and a freshly uploaded attachment travel to the target,
        so the original sender's name can never leak into the target chat.

        Payload::

            {"cmd_id", "source_chat_id", "source_message_id", "target_chat_id",
             "target_tg_chat_id", "message_id", "text", "attachment_path",
             "attachment_kind", "reply_to_tg_msg_id"}

        The ACK is published on ``tg_outgoing_ack`` exactly like a normal send
        (``chat_id`` = ``target_chat_id``, ``message_id`` = the new row), so the
        backend can clear ``pending`` either way.
        """
        if message.get("type") != "message":
            return
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            LOG.error("tg_forward payload is not text (type=%s)", type(data).__name__)
            return

        try:
            command = json.loads(data)
        except json.JSONDecodeError as exc:
            LOG.error("tg_forward payload is not valid JSON (%s): %s", exc, data[:300])
            return
        if not isinstance(command, dict):
            LOG.error("tg_forward payload must be a JSON object, got %s",
                      type(command).__name__)
            return

        cmd_id = str(command.get("cmd_id") or "") or None
        target_chat_id = to_int_or_none(command.get("target_chat_id"))
        message_id = to_int_or_none(command.get("message_id"))

        # 1) Mandatory fields — a negative ack is only possible when we know
        #    the CRM row (message_id); without it we cannot even report back.
        missing = [
            name for name, value in (
                ("cmd_id", cmd_id),
                ("target_chat_id", target_chat_id),
                ("message_id", message_id),
            ) if not value
        ]
        if missing:
            LOG.warning(
                "tg_forward cmd=%s rejected: missing required field(s) %s — "
                "cannot forward (source_chat=%s source_msg=%s)",
                cmd_id, ", ".join(missing),
                command.get("source_chat_id"), command.get("source_message_id"),
            )
            if message_id is not None:
                await self._ack(cmd_id, target_chat_id, message_id, None, False,
                                f"ValueError: missing required field(s): {', '.join(missing)}")
            return

        text = command.get("text")
        body = text if isinstance(text, str) else ""
        reply_to_tg_msg_id = to_int_or_none(command.get("reply_to_tg_msg_id"))
        raw_attachment = command.get("attachment_path")

        attachment_path: Optional[Path] = None
        if raw_attachment is not None and str(raw_attachment).strip():
            candidate = Path(str(raw_attachment).strip().strip("\"'"))
            if not candidate.is_absolute():
                await self._ack(
                    cmd_id, target_chat_id, message_id, None, False,
                    f"ValueError: attachment_path must be absolute ({candidate})",
                )
                return
            if not candidate.is_file():
                LOG.error("forward attachment missing on disk: %s (cmd=%s)", candidate, cmd_id)
                await self._ack(
                    cmd_id, target_chat_id, message_id, None, False,
                    f"FileNotFoundError: attachment_path does not exist ({candidate.name})",
                )
                return
            attachment_path = candidate

        if attachment_path is not None:
            # Caption limit (1024) applies instead of the 4096 body limit.
            if body and len(body) > TG_MAX_FILE_CAPTION_LENGTH:
                await self._ack(
                    cmd_id, target_chat_id, message_id, None, False,
                    f"ValueError: caption too long ({len(body)} > "
                    f"{TG_MAX_FILE_CAPTION_LENGTH} chars for a file)",
                )
                return
        else:
            if not body.strip():
                await self._ack(cmd_id, target_chat_id, message_id, None, False,
                                "ValueError: empty text")
                return
            if len(body) > TG_MAX_MESSAGE_LENGTH:
                await self._ack(
                    cmd_id, target_chat_id, message_id, None, False,
                    f"ValueError: text too long ({len(body)} > {TG_MAX_MESSAGE_LENGTH} chars)",
                )
                return

        # 2) Target entity: the Telegram id wins, the CRM id is the legacy
        #    fallback used by the rest of the worker.
        target_tg_chat_id = to_int_or_none(command.get("target_tg_chat_id"))
        entity_id = target_tg_chat_id if target_tg_chat_id is not None else target_chat_id

        client = self.client
        if client is None or not self._connected:
            await self._ack(cmd_id, target_chat_id, message_id, None, False,
                            "ConnectionError: telegram client is not connected")
            return

        if target_tg_chat_id is None:
            LOG.warning(
                "tg_forward cmd=%s has no target_tg_chat_id; falling back to "
                "target_chat_id=%s as a Telegram id", cmd_id, target_chat_id,
            )

        try:
            entity = await self._resolve_entity(client, entity_id)
        except Exception as exc:
            LOG.error("tg_forward cmd=%s: cannot resolve entity %s (%s: %s)",
                      cmd_id, entity_id, type(exc).__name__, exc)
            await self._ack(cmd_id, target_chat_id, message_id, None, False,
                            f"{type(exc).__name__}: {exc}")
            return

        # 3) Send as our own message. ``_send_forward_copy`` never uses any
        #    Telegram forward primitive.
        try:
            sent = await asyncio.wait_for(
                self._send_forward_copy(
                    client, entity, attachment_path, body=body,
                    reply_to=reply_to_tg_msg_id or None,
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOG.error("forward timed out after %ss (cmd=%s target=%s)",
                      SEND_TIMEOUT_SECONDS, cmd_id, entity_id)
            await self._ack(cmd_id, target_chat_id, message_id, None, False,
                            f"TimeoutError: forward exceeded {SEND_TIMEOUT_SECONDS}s")
            return
        except FloodWaitError as exc:
            wait_seconds = min(int(getattr(exc, "seconds", 0) or 0), FLOOD_WAIT_CAP_SECONDS)
            LOG.warning("FloodWaitError on forward to %s: telegram asks for %ss (capping at %ss)",
                        entity_id, getattr(exc, "seconds", "?"), wait_seconds)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            await self._ack(cmd_id, target_chat_id, message_id, None, False,
                            f"FloodWaitError: {exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("forward to %s failed (cmd=%s): %s: %s",
                      entity_id, cmd_id, type(exc).__name__, exc)
            await self._ack(cmd_id, target_chat_id, message_id, None, False,
                            f"{type(exc).__name__}: {exc}")
            return

        forwarded_id = to_int_or_none(getattr(sent, "id", None))

        # 4) Echo suppression: the re-sent copy triggers ``events.NewMessage``
        #    for our own account; it must not be ingested as a new client
        #    message (the backend already stores it as OPERATOR).
        echo_text = body
        if echo_text:
            register_pending_send(entity_id, echo_text)
            register_pending_send(target_chat_id, echo_text)

        bus = self.bus
        if bus is not None:
            bus.stats.forwarded += 1

        LOG.info(
            "FORWARD cmd=%s src_chat=%s src_msg=%s -> chat=%s msg=%s tg_msg_id=%s",
            cmd_id, command.get("source_chat_id"), command.get("source_message_id"),
            target_chat_id, message_id, forwarded_id,
        )
        await self._ack(cmd_id, target_chat_id, message_id, forwarded_id, True, None,
                        attachment_sent=attachment_path is not None)

    async def _send_forward_copy(
        self,
        client: TelegramClient,
        entity: Any,
        attachment_path: Optional[Path],
        *,
        body: str,
        reply_to: Optional[int],
    ) -> Any:
        """Deliver a forward as a brand-new message from our own account.

        CONTRACTS_V3 §1.1/§5.2 invariant: the worker **never** calls
        ``forward_messages``, ``ForwardMessagesRequest`` or builds a
        ``MessageFwdHeader`` — all of them would render "Forwarded from …"
        in the target chat and leak the original sender. The text is copied
        as our own body and the attachment is uploaded again as a fresh file.

        Raises:
            ValueError: when the attachment is unreadable or empty.
        """
        if attachment_path is not None:
            return await self._send_attachment(
                client, entity, attachment_path,
                caption=body or None, reply_to=reply_to,
            )
        return await client.send_message(
            entity=entity,
            message=body,
            reply_to=reply_to,
            parse_mode=None,
            link_preview=False,
        )

    async def _send_attachment(
        self,
        client: TelegramClient,
        entity: Any,
        attachment_path: Path,
        *,
        caption: Optional[str],
        reply_to: Optional[int],
    ) -> Any:
        """Upload one file with ``send_file`` (CONTRACTS_V2 §5).

        ``parse_mode=None`` keeps the caption literal: the CRM stores the
        operator's text verbatim, so Markdown characters must not be
        interpreted by Telegram (and a stray ``*`` must not fail the send).

        Raises:
            ValueError: when the file is unreadable or empty.
        """
        try:
            size = attachment_path.stat().st_size
        except OSError as exc:
            raise ValueError(
                f"cannot read attachment {attachment_path.name}: {exc}"
            ) from exc
        if size <= 0:
            raise ValueError("attachment is empty (0 bytes)")

        return await client.send_file(
            entity=entity,
            file=str(attachment_path),
            caption=caption or None,
            reply_to=reply_to,
            parse_mode=None,
            force_document=False,
            file_size=size,
        )

    async def _handle_reaction_command(self, message: dict[str, Any]) -> None:
        """Parse & execute one ``tg_reaction_out`` pub/sub message (CONTRACTS_V4 §2).

        Payload::

            {"cmd_id", "action": "SET"|"CLEAR", "message_id", "chat_id",
             "tg_chat_id", "tg_msg_id", "emoji", "operator_username"}

        Telegram stores **one reaction per account per message**, so ``SET``
        replaces whatever our account had there and ``CLEAR`` removes it. A
        reaction placed by the merchant's own account is untouched — only ours
        is addressed.

        The emoji is validated against :data:`SUPPORTED_REACTIONS` *before* the
        RPC (§4.3): Telegram rejects unknown sets with a generic error, while a
        local check produces an actionable message for the CRM log.

        Acks go to ``tg_outgoing_ack`` like every other command, echoing the
        message id the reaction was applied to (not a new message id).
        CONTRACTS_V5 §1.5 tags them with ``cmd_kind="REACTION"``; consumers
        written before V5 simply ignore the extra key.
        """
        if message.get("type") != "message":
            return
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            LOG.error("tg_reaction_out payload is not text (type=%s)", type(data).__name__)
            return

        try:
            command = json.loads(data)
        except json.JSONDecodeError as exc:
            LOG.error("tg_reaction_out payload is not valid JSON (%s): %s", exc, data[:300])
            return
        if not isinstance(command, dict):
            LOG.error("tg_reaction_out payload must be a JSON object, got %s",
                      type(command).__name__)
            return

        cmd_id = str(command.get("cmd_id") or "") or None
        chat_id = to_int_or_none(command.get("chat_id"))
        message_id = to_int_or_none(command.get("message_id"))
        tg_chat_id = to_int_or_none(command.get("tg_chat_id"))
        tg_msg_id = to_int_or_none(command.get("tg_msg_id"))
        action = str(command.get("action") or "").strip().upper()
        raw_emoji = command.get("emoji")

        if message_id is None or tg_chat_id is None or tg_msg_id is None:
            LOG.error("tg_reaction_out is missing chat/message ids: %s",
                      _safe_preview(command))
            return

        if action not in ("SET", "CLEAR"):
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"ValueError: unsupported action {action!r}",
                            cmd_kind="REACTION")
            return

        if action == "SET":
            emoji = normalize_reaction_emoji(raw_emoji)
            if emoji is None:
                await self._ack(
                    cmd_id, chat_id, message_id, tg_msg_id, False,
                    f"ValueError: unsupported reaction {raw_emoji!r} "
                    f"(allowed: {', '.join(sorted(SUPPORTED_REACTIONS))})",
                    cmd_kind="REACTION",
                )
                return
            reaction: list[Any] = [ReactionEmoji(emoticon=emoji)]
        else:
            emoji = None
            # An empty list is Telegram's "remove my reaction".
            reaction = []

        client = self.client
        if client is None or not client.is_connected():
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            "ConnectionError: Telegram client is not connected",
                            cmd_kind="REACTION")
            return

        LOG.info("REACTION cmd=%s chat=%s msg=%s action=%s emoji=%s",
                 cmd_id, tg_chat_id, tg_msg_id, action, emoji or "-")
        try:
            entity = await self._resolve_entity(client, tg_chat_id)
            await asyncio.wait_for(
                client(SendReactionRequest(
                    peer=entity,
                    msg_id=tg_msg_id,
                    reaction=reaction,
                )),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOG.error("reaction timed out after %ss (cmd=%s chat=%s msg=%s)",
                      SEND_TIMEOUT_SECONDS, cmd_id, tg_chat_id, tg_msg_id)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"TimeoutError: reaction exceeded {SEND_TIMEOUT_SECONDS}s",
                            cmd_kind="REACTION")
            return
        except FloodWaitError as exc:
            LOG.warning("reaction flood wait %ss (cmd=%s)", exc.seconds, cmd_id)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"FloodWaitError: retry after {exc.seconds}s",
                            cmd_kind="REACTION")
            return
        except RPCError as exc:
            # ``REACTION_INVALID`` / ``MESSAGE_ID_INVALID`` / no permission: the CRM
            # keeps its own copy, so this is reported, not retried.
            LOG.error("reaction failed (cmd=%s chat=%s msg=%s): %s: %s",
                      cmd_id, tg_chat_id, tg_msg_id, type(exc).__name__, exc)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"{type(exc).__name__}: {exc}", cmd_kind="REACTION")
            return
        except Exception as exc:  # noqa: BLE001 - never kill the pump
            LOG.exception("unexpected error setting a reaction (cmd=%s)", cmd_id)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"{type(exc).__name__}: {exc}", cmd_kind="REACTION")
            return

        if self.bus is not None:
            self.bus.stats.reactions += 1
        await self._ack(cmd_id, chat_id, message_id, tg_msg_id, True, None,
                        cmd_kind="REACTION")

    async def _handle_delete_command(self, message: dict[str, Any]) -> None:
        """Parse & execute one ``tg_delete_out`` pub/sub message (CONTRACTS_V5 §1.4).

        Payload::

            {"cmd_id", "message_id", "chat_id", "tg_chat_id", "tg_msg_id",
             "operator_username"}

        Deletion travels **only** CRM -> Telegram (§1.1); the opposite direction
        stays in ``tg_ingest.handle_deleted`` and is not touched here. Neither
        side ever erases a ``group_messages`` row (§1.2, §8.1) — this handler
        works exclusively with Telegram.

        The RPC is the ``channels`` flavour of ``DeleteMessages``
        (``channel=<peer>, id=[tg_msg_id]``, §1.6). ``channel`` accepts an entity
        *or* a raw ``InputPeerChannel``, so keeping the peer an entity resolved
        through :meth:`_resolve_entity` is correct for both supergroups and
        forums.

        A message that Telegram already considers gone (``MessageDeleteForbiddenError``
        — service message / already removed / owned by someone else) is reported
        as ``ok: true``: the operator asked for it to be gone and it is gone, so
        the CRM must stop showing a "not delivered" hint (§1.6).

        The ACK is published on ``tg_outgoing_ack`` in the §1.5 shape::

            {"cmd_id", "cmd_kind": "DELETE", "message_id", "tg_msg_id",
             "ok", "error"}
        """
        if message.get("type") != "message":
            return
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            LOG.error("tg_delete_out payload is not text (type=%s)", type(data).__name__)
            return

        try:
            command = json.loads(data)
        except json.JSONDecodeError as exc:
            LOG.error("tg_delete_out payload is not valid JSON (%s): %s", exc, data[:300])
            return
        if not isinstance(command, dict):
            LOG.error("tg_delete_out payload must be a JSON object, got %s",
                      type(command).__name__)
            return

        cmd_id = str(command.get("cmd_id") or "") or None
        chat_id = to_int_or_none(command.get("chat_id"))
        message_id = to_int_or_none(command.get("message_id"))
        tg_chat_id = to_int_or_none(command.get("tg_chat_id"))
        tg_msg_id = to_int_or_none(command.get("tg_msg_id"))

        # 1) Validation (§1.4). ``cmd_id``/``tg_chat_id``/``tg_msg_id`` are
        #    mandatory and must be int-castable; a bad payload still gets a
        #    negative ack whenever the CRM row is known, because the backend
        #    flips the row to ``failed`` only on ``ok: false``.
        missing = [
            name for name, value in (
                ("cmd_id", cmd_id),
                ("tg_chat_id", tg_chat_id),
                ("tg_msg_id", tg_msg_id),
            ) if not value
        ]
        if missing:
            LOG.warning(
                "tg_delete_out cmd=%s rejected: missing/invalid required field(s) %s "
                "(chat=%s msg=%s) — cannot delete",
                cmd_id, ", ".join(missing), tg_chat_id, tg_msg_id,
            )
            if message_id is not None:
                await self._ack(
                    cmd_id, chat_id, message_id, tg_msg_id, False,
                    f"ValueError: missing required field(s): {', '.join(missing)}",
                    cmd_kind="DELETE",
                )
            return

        client = self.client
        if client is None or not client.is_connected():
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            "ConnectionError: Telegram client is not connected",
                            cmd_kind="DELETE")
            return

        LOG.info("DELETE cmd=%s chat=%s msg=%s", cmd_id, tg_chat_id, tg_msg_id)
        try:
            peer = await self._resolve_entity(client, tg_chat_id)
            await asyncio.wait_for(
                self._delete_rpc(client, peer, tg_msg_id),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except MessageDeleteForbiddenError as exc:
            # Already deleted, a service message, or not ours any more: the goal
            # ("it must not be visible in Telegram") is reached either way.
            LOG.info("DELETE cmd=%s chat=%s msg=%s: already gone in Telegram (%s)",
                     cmd_id, tg_chat_id, tg_msg_id, type(exc).__name__)
        except asyncio.TimeoutError:
            LOG.error("delete failed (cmd=%s chat=%s msg=%s): timeout after %ss",
                      cmd_id, tg_chat_id, tg_msg_id, SEND_TIMEOUT_SECONDS)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"TimeoutError: delete exceeded {SEND_TIMEOUT_SECONDS}s",
                            cmd_kind="DELETE")
            return
        except FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            LOG.error("delete failed (cmd=%s chat=%s msg=%s): FloodWaitError %ss",
                      cmd_id, tg_chat_id, tg_msg_id, seconds)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"FloodWaitError: retry after {seconds}s",
                            cmd_kind="DELETE")
            return
        except RPCError as exc:
            LOG.error("delete failed (cmd=%s chat=%s msg=%s): %s: %s",
                      cmd_id, tg_chat_id, tg_msg_id, type(exc).__name__, exc)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"{type(exc).__name__}: {exc}", cmd_kind="DELETE")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never kill the pump
            LOG.error("delete failed (cmd=%s chat=%s msg=%s): %s: %s",
                      cmd_id, tg_chat_id, tg_msg_id, type(exc).__name__, exc)
            await self._ack(cmd_id, chat_id, message_id, tg_msg_id, False,
                            f"{type(exc).__name__}: {exc}", cmd_kind="DELETE")
            return

        if self.bus is not None:
            self.bus.stats.delete_out += 1
        await self._ack(cmd_id, chat_id, message_id, tg_msg_id, True, None,
                        cmd_kind="DELETE")

    async def _delete_rpc(
        self, client: Any, peer: Any, tg_msg_id: int
    ) -> Any:
        """Send ``channels.DeleteMessages(channel=peer, id=[tg_msg_id])`` (§1.6).

        Extracted so the request is built in exactly one place: the ``channels``
        flavour is required for supergroups/forums (the ``messages`` flavour only
        addresses private chats and basic groups), and ``channel`` takes the
        entity/handle returned by :meth:`_resolve_entity` verbatim.

        The request object travels to ``client(request)`` — Telethon's
        ``__call__`` — which makes the call trivially observable in tests. A
        client lookalike that only exposes ``__call__`` (no ``__getattr__``) is
        supported as well, because ``client(...)`` is the documented API and
        nothing else is needed here.
        """
        request = DeleteMessagesRequest(channel=peer, id=[tg_msg_id])
        call = getattr(client, "__call__", None)
        if call is None:  # pragma: no cover - defensive, Telethon always has it
            raise TypeError("Telegram client is not callable")
        return await call(request)

    async def _resolve_entity(self, client: TelegramClient, tg_chat_id: int) -> Any:
        """Return a sendable entity for ``tg_chat_id`` (numeric send works too).

        Sending by a raw integer id is supported by Telethon for groups (it
        builds the right ``InputPeerChannel``), but resolving once and caching
        avoids an ``access_hash`` lookup on every message and surfaces a
        meaningful error when the account is no longer a member.

        Raises:
            ValueError: when the account cannot resolve the entity at all.
        """
        cached = self._entity_cache.get(tg_chat_id)
        if cached is not None:
            self._entity_cache.move_to_end(tg_chat_id)
            return cached
        try:
            entity = await client.get_entity(tg_chat_id)
        except Exception as exc:
            LOG.warning("get_entity(%s) failed (%s: %s); sending by raw id anyway",
                        tg_chat_id, type(exc).__name__, exc)
            return tg_chat_id
        self._entity_cache[tg_chat_id] = entity
        while len(self._entity_cache) > self._entity_cache_max:
            self._entity_cache.popitem(last=False)
        return entity

    async def _ack(
        self,
        cmd_id: Optional[str],
        chat_id: Optional[int],
        message_id: Optional[int],
        tg_msg_id: Optional[int],
        ok: bool,
        error: Optional[str],
        *,
        attachment_sent: bool = False,
        cmd_kind: Optional[str] = None,
    ) -> None:
        """Publish one ``tg_outgoing_ack`` (exact §5 shape, never raises).

        CONTRACTS_V2 §5 adds ``attachment_sent``: ``true`` when the command
        was delivered with ``send_file``. It is only emitted for successful
        file sends so text-only acks keep the exact legacy shape; a failed
        command reports ``false``.

        CONTRACTS_V5 §1.5 adds ``cmd_kind`` (``"DELETE"`` / ``"REACTION"``).
        It is emitted **only** when the caller passes one, so the plain-send
        ACK stays byte-identical to the legacy shape; older consumers simply
        ignore the extra key when it is present.
        """
        payload: dict[str, Any] = {
            "cmd_id": cmd_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "tg_msg_id": tg_msg_id,
            "ok": bool(ok),
            "error": error,
        }
        if attachment_sent:
            payload["attachment_sent"] = True
        if cmd_kind is not None:
            payload["cmd_kind"] = cmd_kind
        bus = self.bus
        if bus is None:
            LOG.error("Redis bus unavailable — ack dropped: %s", _safe_preview(payload))
            return
        if await bus.publish(CHANNEL_OUTGOING_ACK, payload):
            if ok:
                bus.stats.acks += 1
            else:
                bus.stats.failures += 1
            LOG.info("ACK cmd=%s kind=%s ok=%s tg_msg_id=%s error=%s",
                     cmd_id, cmd_kind or "-", ok, tg_msg_id, (error or "")[:200])

    # ------------------------------------------------------------------ #
    # Heartbeat
    # ------------------------------------------------------------------ #

    async def heartbeat_loop(self) -> None:
        """Publish the worker's liveness every ``cfg.heartbeat_seconds``.

        Writes::

            worker:status     hash  connected/updated_at/worker/monitored_chats/stats
            worker:heartbeat  str   ISO timestamp, EX = heartbeat * 3 (liveness)

        The backend `/api/system/info` reads these to report
        ``telegram_connected``.
        """
        redis_client = self.redis
        if redis_client is None:
            raise RuntimeError("heartbeat_loop started before Redis was initialised")

        interval = max(5, int(self.cfg.heartbeat_seconds))
        LOG.info("heartbeat loop started (every %ss)", interval)
        while not self.stopping:
            try:
                await self._beat_once(redis_client)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                LOG.warning("heartbeat write failed: %s: %s", type(exc).__name__, exc)
            except Exception:
                LOG.exception("heartbeat iteration crashed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
        LOG.info("heartbeat loop stopped")

    async def _beat_once(self, redis_client: "aioredis.Redis") -> None:
        """Write one heartbeat sample."""
        stamp = utc_iso()
        stats = self.bus.stats if self.bus is not None else _PublishStats()
        mapping = {
            "connected": "1" if self._connected else "0",
            "updated_at": stamp,
            "worker": self.cfg.worker_name or "",
            "monitored_chats": self._monitored_json,
            "connected_since_uptime": f"{int(time.monotonic() - self._started_at)}",
            "incoming": str(stats.incoming),
            "deleted": str(stats.deleted),
            "edited": str(stats.edited),
            "acks": str(stats.acks),
            "failures": str(stats.failures),
            "reactions": str(stats.reactions),
            "media_downloaded": str(stats.media_downloaded),
            "media_failed": str(stats.media_failed),
            "attachments_sent": str(stats.attachments_sent),
            "forwarded": str(stats.forwarded),
            "delete_out": str(stats.delete_out),
            "media_root": str(self._media_root),
            "max_download_bytes": str(self._max_download_bytes),
        }
        await redis_client.hset(REDIS_STATUS_KEY, mapping=mapping)
        await redis_client.set(
            REDIS_HEARTBEAT_KEY, stamp, ex=max(5, int(self.cfg.heartbeat_seconds) * 3)
        )
        LOG.debug("heartbeat %s connected=%s", stamp, mapping["connected"])

    async def _mark_disconnected(self, *, reason: str) -> None:
        """Graceful shutdown marker: ``connected=0`` and drop the TTL key."""
        self._connected = False
        redis_client = self.redis
        if redis_client is None:
            LOG.info("shutdown (%s): no Redis connection to update", reason)
            return
        try:
            await redis_client.hset(
                REDIS_STATUS_KEY,
                mapping={
                    "connected": "0",
                    "updated_at": utc_iso(),
                    "worker": self.cfg.worker_name or "",
                },
            )
            await redis_client.delete(REDIS_HEARTBEAT_KEY)
            LOG.info("shutdown (%s): worker:status.connected=0, heartbeat key removed", reason)
        except (RedisError, OSError) as exc:
            LOG.warning("could not update Redis on shutdown: %s: %s", type(exc).__name__, exc)
        except Exception:
            LOG.exception("unexpected error updating Redis on shutdown")

    # ------------------------------------------------------------------ #
    # Redis bootstrap
    # ------------------------------------------------------------------ #

    async def _connect_redis(self, *, required: bool = True) -> Optional["aioredis.Redis"]:
        """Create the Redis client and ``PING`` it with backoff.

        Args:
            required: When ``True`` a failure raises ``SystemExit`` after the
                ~30 s budget (fail fast, but survivable for slow containers).
                When ``False`` (dry-run) the error is returned as ``None``.

        Raises:
            SystemExit: if Redis stays unreachable past the budget.
        """
        url = self.cfg.redis_url
        try:
            redis_client: "aioredis.Redis" = aioredis.from_url(
                url,
                decode_responses=True,
                health_check_interval=30,
                socket_keepalive=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                retry_on_timeout=True,
            )
        except Exception as exc:
            message = f"invalid REDIS_URL ({_redis_endpoint_summary(url)}): {type(exc).__name__}: {exc}"
            if required:
                raise SystemExit(f"[redis] {message}") from exc
            LOG.error("%s", message)
            return None

        deadline = time.monotonic() + REDIS_CONNECT_BUDGET_SECONDS
        delay = 0.5
        attempt = 0
        last_error: Optional[BaseException] = None
        while True:
            attempt += 1
            try:
                await asyncio.wait_for(redis_client.ping(), timeout=5.0)
                LOG.info("Redis connected: %s (attempt %d)", _redis_endpoint_summary(url), attempt)
                return redis_client
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                pause = min(delay, remaining)
                LOG.warning(
                    "Redis ping failed (attempt %d): %s: %s — retrying in %.1fs",
                    attempt, type(exc).__name__, exc, pause,
                )
                await asyncio.sleep(pause)
                delay = min(delay * 2, 5.0)

        try:
            await redis_client.aclose()
        except AttributeError:  # pragma: no cover - redis 4.x
            try:
                await redis_client.close()  # type: ignore[attr-defined]
            except Exception:
                LOG.debug("redis close failed", exc_info=True)
        except Exception:
            LOG.debug("redis close failed", exc_info=True)

        detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown error"
        message = (
            f"Redis is unreachable at {_redis_endpoint_summary(url)} after "
            f"{REDIS_CONNECT_BUDGET_SECONDS}s ({attempt} attempts): {detail}. "
            "Start it with `docker compose -f docker-compose.dev.yml up -d redis` "
            "and check REDIS_URL in .env."
        )
        if required:
            raise SystemExit(f"[redis] {message}")
        LOG.error("%s", message)
        return None

    # ------------------------------------------------------------------ #
    # Session bootstrap (first run only)
    # ------------------------------------------------------------------ #

    @property
    def _session_source(self) -> str:
        """Where the session comes from, for diagnostics: ``string`` or ``file``."""
        if self._runtime_session_string:
            return "string (created on this run)"
        return "string" if self.cfg.has_session_string else "file"

    async def interactive_login(self, client: TelegramClient) -> Optional[str]:
        """Log in interactively and return the resulting ``StringSession``.

        Used by ``--login`` and by the first-run path (no session string and
        no session file). Prompts use ``input``/``getpass`` — the only
        permitted blocking I/O in this file — and the resulting string is
        printed so the operator can paste it into ``.env`` as
        ``TG_SESSION_STRING``.

        Returns:
            The session string, or ``None`` if the login failed.
        """
        banner = "\n".join(
            [
                "",
                "+" + "-" * 76 + "+",
                "|  FIRST RUN — no Telegram session found." + " " * 34 + "|",
                "|" + " " * 76 + "|",
                "|  The worker will now log in to the corporate Telegram account." + " " * 9 + "|",
                "|  You will need: the phone number (+7…), the login code from" + " " * 15 + "|",
                "|  Telegram (or the SMS) and, if enabled, the 2FA password." + " " * 18 + "|",
                "|" + " " * 76 + "|",
                "|  The generated session string is printed at the end — copy it" + " " * 12 + "|",
                "|  into TG_STRING_SESSION in the repository-root .env file." + " " * 16 + "|",
                "+" + "-" * 76 + "+",
                "",
            ]
        )
        # Interactive UI, not runtime logging: print is intentional here.
        print(banner, flush=True)
        LOG.info("starting interactive Telegram login (no session configured)")

        try:
            await client.start(
                phone=lambda: input("Phone (+...): ").strip(),
                password=lambda: getpass.getpass("2FA password: "),
                code_callback=lambda: input("Login code (from Telegram): ").strip(),
            )
        except EOFError:
            LOG.error("interactive login aborted: no console input available")
            return None
        except Exception as exc:
            LOG.error("interactive login failed: %s: %s", type(exc).__name__, exc)
            return None

        session_string = ""
        try:
            session = client.session
            session_string = str(session.save() or "")
        except Exception:
            LOG.exception("could not serialise the session")
            return None
        if not session_string:
            LOG.error("session saved empty — check the Telethon version")
            return None

        block = "\n".join(
            [
                "",
                "=" * 78,
                "  SESSION CREATED".center(78),
                "=" * 78,
                "  Copy the value below into .env (single line, no quotes):",
                "",
                "  TG_SESSION_STRING=" + session_string,
                "",
                "  The worker keeps this session in memory and keeps running;",
                "  store the value in .env so the next start needs no login.",
                "=" * 78,
                "",
            ]
        )
        print(block, flush=True)
        return session_string

    # ------------------------------------------------------------------ #
    # Dry-run / diagnostics
    # ------------------------------------------------------------------ #

    async def list_chats(self, client: TelegramClient, *, limit: int = 500) -> list[dict[str, Any]]:
        """Iterate the account's dialogs and return the groups it could bridge.

        Only "polylog-safe" attributes are read (id/title/username/type), so
        no extra network round-trip is triggered per dialog. Broadcast
        channels and private chats are excluded because the worker ignores
        them by contract; ``tg_chat_id`` is rendered via
        ``utils.get_peer_id`` (the ``-100…`` form the backend stores).
        """
        rows: list[dict[str, Any]] = []
        try:
            async for dialog in client.iter_dialogs(limit=limit):
                entity = getattr(dialog, "entity", None)
                if not isinstance(entity, (Channel, Chat)):
                    continue  # skip users / bots / saved messages
                is_megagroup = bool(getattr(entity, "megagroup", False))
                if isinstance(entity, Channel) and not is_megagroup:
                    continue  # broadcast channel -> not bridged
                title = getattr(entity, "title", None) or ""
                username = getattr(entity, "username", None) or None
                effective_id = to_int_or_none(tg_utils.get_peer_id(entity))
                if effective_id is None:
                    effective_id = to_int_or_none(getattr(entity, "id", None))
                rows.append(
                    {
                        "tg_chat_id": effective_id,
                        "title": title or str(effective_id),
                        "username": username,
                        "type": "supergroup" if is_megagroup else "group",
                        "merchant_code": derive_merchant_code(
                            title=title, chat_username=username
                        ),
                        "monitored": self.is_chat_monitored(effective_id, title, username),
                    }
                )
        except Exception:
            LOG.exception("failed to enumerate dialogs")
        return rows

    async def dry_run(self) -> int:
        """Connect (no Redis writes) and report the chats that would be bridged.

        Returns:
            Process exit code (0 = credentials work, 1 = login/connect failed).
        """
        LOG.info("DRY-RUN: credentials + entity smoke test, Redis is NOT written to")
        client = self._new_client()
        try:
            ok = await self._start_client(client, allow_interactive=False)
            if not ok:
                LOG.error("DRY-RUN failed: could not connect/authorise the Telegram session")
                return 1

            try:
                me = await client.get_me()
            except Exception as exc:
                LOG.error("DRY-RUN: get_me() failed: %s: %s", type(exc).__name__, exc)
                me = None
            if me is not None:
                who = f"{getattr(me, 'first_name', '') or ''} {getattr(me, 'last_name', '') or ''}".strip()
                username = getattr(me, "username", None)
                LOG.info("DRY-RUN authorised as %s (@%s) tg_user_id=%s",
                         who or "<no name>", username or "-", getattr(me, "id", "-"))

            rows = await self.list_chats(client)
            monitored = [row for row in rows if row["monitored"]]
            LOG.info("DRY-RUN dialogs found=%d, monitored=%d", len(rows), len(monitored))
            for row in rows:
                marker = "MONITOR" if row["monitored"] else "skip   "
                LOG.info(
                    "  %s  id=%-16s %-14s %-32s username=%-24s code=%s",
                    marker,
                    row["tg_chat_id"],
                    row["type"],
                    (row["title"] or "")[:32],
                    (row["username"] or "-")[:24],
                    row["merchant_code"] or "-",
                )
            if not monitored:
                LOG.warning(
                    "DRY-RUN: no dialogs match monitored_chat_ids=%s. Either join the "
                    "groups with this account or fix TG_MONITORED_CHAT_IDS.",
                    self.cfg.monitored_chat_ids,
                )
            LOG.info("DRY-RUN OK — the session works; you can start the worker.")
            return 0
        finally:
            await self._disconnect_client(client, reason="dry-run")

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    async def _start_client(self, client: TelegramClient, *, allow_interactive: bool) -> bool:
        """Connect + authorise ``client``; bootstrap a session only on first run.

        Session-state machine:

        ``StringSession`` (``TG_SESSION_STRING``) — never prompts. An
        unauthorised/dead string is a hard configuration error: the operator
        must regenerate it with ``--login``.

        ``SQLiteSession`` file — prompts interactively only when the file is
        missing/empty (telethon cannot know whether a fresh session is
        authorised, so ``is_user_authorized()`` is the deciding check).

        Returns:
            ``True`` when the client is ready to receive events.
        """
        await client.connect()

        if await client.is_user_authorized():
            LOG.info("Telegram session authorised (source=%s)", self._session_source)
            return True

        if self.cfg.has_session_string:
            LOG.error(
                "TG_SESSION_STRING was rejected (expired, revoked or corrupted). "
                "Regenerate it with `python worker.py --login`; refusing to prompt "
                "because a string session must stay non-interactive."
            )
            return False

        if not allow_interactive:
            LOG.error("Telegram session is not authorised and interactive login is disabled")
            return False

        session_string = await self.interactive_login(client)
        if session_string is None:
            return False
        # Keep the fresh session in memory so the supervisor's reconnect reuses it
        # without prompting again; .env is still the operator's to update.
        self._runtime_session_string = session_string
        LOG.info(
            "first-run login succeeded — paste the printed TG_SESSION_STRING into "
            ".env to make future starts non-interactive"
        )
        return True

    async def _disconnect_client(self, client: Optional[TelegramClient], *, reason: str) -> None:
        """Best-effort disconnect; never raises."""
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=15)
            LOG.info("Telegram client disconnected (%s)", reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Telegram disconnect failed (%s): %s: %s",
                        reason, type(exc).__name__, exc)

    async def _supervise_once(self) -> None:
        """Create a client, run client + loops together, tear everything down."""
        client = self._new_client()
        self.client = client
        tasks: list[asyncio.Task[Any]] = []

        try:
            await self._start_client(client, allow_interactive=True)
            self._connected = True
            self._entity_cache.clear()
            LOG.info("Telegram connected; bridging chats: %s",
                     "ALL groups" if self.cfg.monitors_all_chats else self.cfg.monitored_chat_ids)

            tasks = [
                asyncio.create_task(self._run(client), name="telegram-client"),
                asyncio.create_task(self.outgoing_loop(), name="outgoing-loop"),
                asyncio.create_task(self.heartbeat_loop(), name="heartbeat-loop"),
            ]
            stop_task = asyncio.create_task(self._stop_event.wait(), name="stop-waiter")
            tasks.append(stop_task)

            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._connected = False
            # The reaction-handler watcher outlives its client: cancel it
            # before the event loop closes, otherwise asyncio prints a
            # "Task was destroyed but it is pending" traceback on reconnect.
            watcher = self._watcher_task
            self._watcher_task = None
            if watcher is not None and not watcher.done():
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            # A new client is created on every reconnect, so the per-client
            # "handler installed" flags must be cleared as well.
            self._raw_handler = None
            self._raw_reactions_installed = False
            self._reaction_event_registered = False
            for task in tasks:
                if task.done():
                    continue
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._write_connected_flag(False)
            await self._disconnect_client(client, reason="teardown")

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                LOG.error("task %s ended with %s: %s",
                          task.get_name(), type(exc).__name__, exc)

    async def _run(self, client: TelegramClient) -> None:
        """Run the Telethon client until it disconnects or we stop."""
        runner = asyncio.create_task(client.run_until_disconnected(), name="run_until_disconnected")
        stop_task = asyncio.create_task(self._stop_event.wait(), name="stop-waiter-run")
        try:
            done, _pending = await asyncio.wait(
                {runner, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        if runner.done() and not runner.cancelled():
            exc = runner.exception()
            if exc is not None:
                LOG.warning("Telegram connection dropped: %s: %s", type(exc).__name__, exc)
            else:
                LOG.warning("Telegram connection closed by the server")

    async def _write_connected_flag(self, connected: bool) -> None:
        """Best-effort ``worker:status.connected`` update during supervision."""
        self._connected = connected
        redis_client = self.redis
        if redis_client is None:
            return
        try:
            await redis_client.hset(
                REDIS_STATUS_KEY,
                mapping={
                    "connected": "1" if connected else "0",
                    "updated_at": utc_iso(),
                    "worker": self.cfg.worker_name or "",
                    "monitored_chats": self._monitored_json,
                },
            )
        except (RedisError, OSError) as exc:
            LOG.debug("connected flag write failed: %s: %s", type(exc).__name__, exc)
        except Exception:
            LOG.debug("connected flag write failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Top-level supervisor
    # ------------------------------------------------------------------ #

    async def run(self) -> int:
        """Supervised main loop with exponential backoff.

        Recreates the Telegram client after every disconnect (network drop,
        server-side termination, expired session). Transient per-session state
        (``pending_crm_sends``, entity cache) is refreshed on every attempt.

        Returns:
            Process exit code.
        """
        redis_client = await self._connect_redis(required=True)
        if redis_client is None:  # pragma: no cover - required=True raises
            return 1
        self.redis = redis_client
        self.bus = Bus(redis_client)

        delay = 1
        attempt = 0
        try:
            while not self.stopping:
                attempt += 1
                LOG.info("supervisor: starting Telegram session (attempt %d)", attempt)
                try:
                    await self._supervise_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.error("supervisor: session crashed: %s: %s", type(exc).__name__, exc)
                    LOG.debug("supervisor traceback", exc_info=True)

                if self.stopping:
                    break

                prune_pending_sends()
                LOG.warning("supervisor: reconnecting in %ss (attempt %d)", delay, attempt)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, max(5, int(self.cfg.reconnect_max_delay)))
            return 0
        finally:
            LOG.info("supervisor: shutting down")
            await self._mark_disconnected(reason="supervisor exit")
            await self._close_redis()

    async def _close_redis(self) -> None:
        """Close pub/sub + the Redis connection pool (never raises)."""
        self.bus = None
        redis_client = self.redis
        self.redis = None
        if redis_client is None:
            return
        try:
            await redis_client.aclose()
            LOG.info("Redis connection closed")
        except AttributeError:  # pragma: no cover - redis 4.x
            try:
                await redis_client.close()  # type: ignore[attr-defined]
                LOG.info("Redis connection closed")
            except Exception:
                LOG.debug("redis close failed", exc_info=True)
        except Exception as exc:
            LOG.debug("redis close failed: %s: %s", type(exc).__name__, exc)


# --------------------------------------------------------------------------- #
# CLI / bootstrap
# --------------------------------------------------------------------------- #


def _configure_logging(level_name: str) -> None:
    """Configure root logging with a timestamped, named format."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # Third-party noise control.
    logging.getLogger("telethon").setLevel(max(level, logging.WARNING))
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    """CLI surface for the worker."""
    parser = argparse.ArgumentParser(
        prog="worker.py",
        description=(
            "TG-Desk CRM Telethon <-> Redis bridge. Watches the corporate Telegram "
            "groups, publishes them to Redis (tg_incoming / tg_deleted / tg_edits / "
            "tg_reactions, with downloaded media), and forwards CRM replies "
            "(tg_outgoing -> tg_outgoing_ack), including file attachments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python worker.py                   # run the bridge (foreground)\n"
            "  python worker.py --login           # create TG_SESSION_STRING\n"
            "  python worker.py --dry-run         # smoke-test credentials, no Redis\n"
            "  python worker.py --list-chats      # show which dialogs would be bridged\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="connect with the current credentials, list the monitored dialogs and exit "
             "WITHOUT touching Redis (safe credentials smoke test)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="override the LOG_LEVEL env var for this run",
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="print every group/supergroup the account can see plus whether it is "
             "monitored, then exit (implies a no-Redis run)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="force the interactive login flow, print a fresh StringSession and exit",
    )
    return parser


async def _login_flow(cfg: WorkerConfig) -> int:
    """``--login`` implementation: interactive login, print the session, exit."""
    client = TelegramClient(
        StringSession(),  # in-memory session, nothing written to disk
        cfg.api_id,
        cfg.api_hash,
        connection_retries=3,
        request_retries=3,
        auto_reconnect=False,
        device_model="TG-Desk CRM Worker",
        system_version="Windows 10",
        app_version="1.0.0",
    )
    try:
        await client.connect()
        worker = TelethonWorker(cfg)
        session_string = await worker.interactive_login(client)
        return 0 if session_string else 1
    except Exception as exc:
        LOG.error("login flow failed: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:
            LOG.debug("login client disconnect failed", exc_info=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Synchronous entry point: parse CLI, load config, run the requested mode.

    Deliberately **not** a coroutine: each mode owns its own ``asyncio.run`` so
    a single event loop is never nested inside another one.
    """
    args = build_parser().parse_args(argv)

    _configure_logging(args.log_level or os.environ.get("LOG_LEVEL", "INFO"))
    LOG.info("TG-Desk Telethon worker starting (pid=%s, python=%s)",
             os.getpid(), sys.version.split()[0])
    LOG.info("cryptg acceleration: %s", "enabled" if _CRYPTG_AVAILABLE else "NOT installed (slower)")
    LOG.info("json codec: %s", "orjson" if orjson is not None else "stdlib json")

    try:
        cfg = load_config(validate_credentials=True)
    except SystemExit as exc:
        LOG.error("configuration error:\n%s", exc)
        return 2

    LOG.info("configuration loaded (repo root: %s)\n%s", REPO_ROOT, cfg.describe())

    if not cfg.has_session_string and not cfg.session_file_exists and not args.login:
        LOG.warning(
            "no session found (TG_SESSION_STRING empty, %s missing): an interactive "
            "login will start. Use --login to only create a session string.",
            cfg.session_path,
        )

    worker = TelethonWorker(cfg)

    if args.login:
        return _run_async_blocking(_login_flow(cfg), label="login")

    if args.dry_run or args.list_chats:
        return _run_async_blocking(worker.dry_run(), label="dry-run")

    return _run_worker(worker)


def _run_async_blocking(coro: Any, *, label: str) -> int:
    """``asyncio.run`` a one-shot coroutine with clean Ctrl+C semantics."""
    try:
        return int(asyncio.run(coro))
    except KeyboardInterrupt:
        LOG.info("%s interrupted by Ctrl+C", label)
        return 130
    except SystemExit as exc:
        LOG.error("%s failed: %s", label, exc)
        return int(getattr(exc, "code", 1) or 1)
    except Exception:
        LOG.exception("%s crashed", label)
        return 1


def _run_worker(worker: TelethonWorker) -> int:
    """Run the async supervisor with OS signal handling (Windows-friendly)."""

    async def runner() -> int:
        loop = asyncio.get_running_loop()

        def _on_signal(signame: str) -> None:
            worker.request_stop(f"signal {signame}")

        # add_signal_handler is unsupported on Windows' ProactorEventLoop:
        # there Ctrl+C raises KeyboardInterrupt, which we catch below.
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _on_signal, sig.name)
                except (NotImplementedError, RuntimeError, ValueError):
                    LOG.debug("add_signal_handler(%s) unavailable", sig.name)
        else:
            for sig_name in ("SIGBREAK",):
                sig = getattr(signal, sig_name, None)
                if sig is None:
                    continue
                try:
                    loop.add_signal_handler(sig, _on_signal, sig_name)
                except (NotImplementedError, RuntimeError, ValueError):
                    LOG.debug("add_signal_handler(%s) unavailable on Windows", sig_name)

        return await worker.run()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt received — worker stopped cleanly")
        return 0
    except asyncio.CancelledError:
        LOG.info("worker cancelled — stopped cleanly")
        return 0
    except SystemExit as exc:
        # Redis unreachable / fatal bootstrap: the message is already logged,
        # so exit with a clean, non-zero code instead of a traceback.
        code = getattr(exc, "code", 1)
        LOG.error("worker exiting: %s", exc or f"exit code {code}")
        return int(code) if isinstance(code, int) else 1
    except Exception:
        LOG.exception("fatal worker error")
        return 1


def _entrypoint() -> int:
    """Top-level guard: delegate to :func:`main`, normalise the exit code."""
    try:
        return main()
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt received — worker stopped cleanly")
        return 0
    except asyncio.CancelledError:
        LOG.info("worker cancelled — stopped cleanly")
        return 0
    except SystemExit as exc:
        LOG.error("%s", exc)
        return int(getattr(exc, "code", 1) or 1)
    except Exception:
        LOG.exception("fatal worker error")
        return 1


if __name__ == "__main__":
    raise SystemExit(_entrypoint())

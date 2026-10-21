"""Deterministic, idempotent database seeder for the TG-Desk CRM backend.

Run from the ``backend`` directory::

    python seed.py                      # upsert everything (safe to re-run)
    python seed.py --reset              # drop_all + create_all, then seed
    python seed.py --operators 25       # more operators (1..999)
    python seed.py --no-history         # operators + chats only (fast smoke test)
    python -m seed --reset --force      # allow --reset with APP_ENV=production

Why it is built this way
------------------------
**Idempotent.** Every row is looked up by its natural key - ``operators.username``,
``group_chats.tg_chat_id``, ``group_messages(chat_id, tg_msg_id)``,
``sla_incidents(chat_id, message_id)`` and
``tx_lookup_audit(operator_id, tx_id, admin_panel)`` - and is only inserted when
missing. A second run duplicates nothing and never rewrites rows that belong to
live traffic.

**Deterministic.** History comes from ``random.Random`` seeded with a fixed salt
per chat, and every timestamp is derived from one anchor, so two seeds of an
empty database produce identical rows (including ``deleted_at`` and the SLA
response times).

**Timezone aware.** Every ``DateTime(timezone=True)`` column is written with an
explicit UTC ``datetime``; ``func.now()`` is never used, so a seeded row means
the same thing regardless of the server clock.

**Internally consistent.** The history, the SLA incidents and the mock admin
panels (``app.services.admin_adapters``) tell one story: a BOT alert quoting
``TX-4412-DD`` with ``Status: FAILED`` is what ``POST /api/tx/lookup`` returns for
that id, so the transaction sidebar never contradicts the transcript.

**Database support.** The production target is PostgreSQL only. A SQLite target
is still supported because it is how the seeder is smoke-tested without a
Postgres server: on SQLite the PostgreSQL-only ``char_length()`` CHECK is
dropped and the 64-bit primary keys are compiled as ``INTEGER`` so SQLite's
``rowid`` aliasing keeps the self-referencing foreign keys working. The DDL that
``PostgreSQL`` receives is never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import sys
import traceback
import warnings
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Final, Literal, Protocol

from sqlalchemy import BigInteger, Integer, func, inspect, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.database import SessionLocal, engine, init_models

# ``OperatorRole`` is imported inside a try/except: it is part of the real model
# module but was not in the frozen import contract handed to this file, so the
# seeder degrades to the literal string instead of failing to import.
from app.enums import (
    DeliveryStatus,
    MessageKind,
    MessageSenderType,
    ReactionEmoji,
    ShiftStatus,
    SlaIncidentStatus,
    TxAdminPanel,
    TxStatus,
)
from app.models import (
    Base,
    DELETED_SOURCE_TELEGRAM,
    GroupChat,
    GroupMessage,
    MediaAttachment,
    MessageReaction,
    Operator,
    QuickReply,
    REACTION_SOURCE_CRM,
    REACTION_SOURCE_TELEGRAM,
    SLAIncident,
    TxLookupAudit,
)
from app.security import hash_password
from app.utils import extract_phone, extract_tx_id

try:  # pragma: no cover - depends on whether the models export the role enum
    from app.models import OperatorRole  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    OperatorRole = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("app.seed")

__all__ = [
    "ADMIN_PASSWORD",
    "ADMIN_USERNAME",
    "AttachmentSpec",
    "AuditSpec",
    "CANONICAL_TX_IDS",
    "CHAT_SPECS",
    "ChatSpec",
    "IncidentSpec",
    "MediaMessageSpec",
    "MessageSpec",
    "OperatorSpec",
    "QUICK_REPLY_SPECS",
    "QuickReplySpec",
    "ReactionSpec",
    "SeedStats",
    "build_attachments",
    "build_audits",
    "build_chats",
    "build_incidents",
    "build_messages",
    "build_operators",
    "build_reactions",
    "main",
    "media_kind_of",
    "seed",
    "seed_sync",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_OPERATOR_COUNT: Final[int] = 10
MAX_OPERATOR_COUNT: Final[int] = 999
TG_MSG_ID_START: Final[int] = 90_000
#: Supergroup ids run ascending: -1001234567890, ...891, ...892, ... (the
#: contract shows the first chat at ...790, so the range is BASE + 0 .. BASE + 5).
TG_CHAT_ID_BASE: Final[int] = -1_001_234_567_890
RANDOM_SEED: Final[int] = 42
SLA_TARGET_SECONDS: Final[int] = 300
PRODUCTION_ENVS: Final[tuple[str, ...]] = ("prod", "production")

ROLE_OPERATOR: Final[str] = "OPERATOR"
ROLE_SUPERVISOR: Final[str] = "SUPERVISOR"
ROLE_ADMIN: Final[str] = "ADMIN"

#: Administrator account seeded on top of the ``.env`` supervisor (CONTRACTS_V2.md
#: §3.3). ``settings.backend_supervisor_username`` defaults to ``"admin"`` as well
#: — when the two collide the supervisor is renamed to :data:`SUPERVISOR_USERNAME`
#: so both roles stay reachable (see :func:`build_operators`).
ADMIN_USERNAME: Final[str] = "admin"
ADMIN_PASSWORD: Final[str] = "admin123"
ADMIN_DISPLAY_NAME: Final[str] = "Администратор"
SUPERVISOR_USERNAME: Final[str] = "supervisor"
SUPERVISOR_COLOR: Final[str] = "#8774e1"
ADMIN_COLOR: Final[str] = "#e17076"

#: Chats whose operator is allowed to delete messages / export transcripts. All
#: other seeded operators are restricted, which makes the two permissions
#: visible in the UI instead of uniformly ``true``.
PRIVILEGED_OPERATOR_USERNAMES: Final[frozenset[str]] = frozenset({"op1", "op3"})

#: Avatar colour ring reused for every seeded operator (CONTRACTS.md §0 palette:
#: tg-accent first, then Telegram-style identity colours).
AVATAR_COLORS: Final[tuple[str, ...]] = (
    "#8774e1",
    "#e17076",
    "#a0de7e",
    "#53edd6",
    "#ffcda5",
    "#72d5fd",
    "#e0a2f3",
    "#faa774",
)

#: Default anchor for the historical window. Live rows (SLA deadlines) are
#: always relative to the wall clock when ``seed()`` runs, but the *history* is
#: pinned to this instant so re-running produces identical data.
SEED_ANCHOR: Final[datetime] = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

#: Transaction ids the frontend/QA screenshots depend on.
CANONICAL_TX_IDS: Final[tuple[str, ...]] = (
    "TX-4412-DD",
    "TX-7781-AC",
    "TX-1102-BE",
    "TX-9931-ZQ",
    "TX-0000-XX",
)

_NUMBER_WORDS: Final[tuple[str, ...]] = (
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
    "Eighteen", "Nineteen", "Twenty",
)

#: Fallback phone matcher, used only when ``app.utils`` publishes no extractor.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9A-Za-z])(\+\d[\d\s().\-]{6,18}\d)(?![0-9A-Za-z])"
)

SenderKind = Literal["OPERATOR", "CLIENT", "BOT", "SYSTEM"]
BotStatus = Literal[
    "COMPLETED", "PROCESSING", "PENDING", "FAILED", "DECLINED", "REFUNDED", "NOT_FOUND"
]
IncidentPlan = Literal["resolved", "due_soon", "burning", "overdue"]


# --------------------------------------------------------------------------- #
# Data contracts produced by the pure builders
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OperatorSpec:
    """One row of ``operators``."""

    username: str
    display_name: str
    role: str
    shift_status: str
    is_online_ws: bool
    sla_target_seconds: int
    #: Minutes before "now" the operator was last seen (0 = right now).
    last_seen_offset: int
    is_supervisor: bool = False
    # -- CONTRACTS_V2.md §2.3 -------------------------------------------------
    is_active: bool = True
    #: Simultaneous-chat cap; ``None`` = unlimited.
    max_chats: int | None = None
    can_delete_messages: bool = False
    can_export: bool = False
    #: ``#RRGGBB`` avatar colour.
    avatar_color: str | None = None
    #: Which seeded password hash this account gets (``admin`` = special case).
    password_kind: Literal["operator", "supervisor", "admin"] = "operator"


@dataclass(frozen=True, slots=True)
class AttachmentSpec:
    """One row of ``media_attachments``.

    ``storage_path``/**``thumbnail_path`` are always derived from
    ``chat_key``/``message_seq`` by :func:`build_attachments` — they are never
    authored by hand, so a seeded row can never point at another message's
    folder.
    """

    kind: str
    file_name: str
    mime_type: str
    size_bytes: int
    #: Path relative to ``settings.media_root`` (POSIX separators).
    storage_path: str
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None
    tg_file_id: str | None = None
    thumbnail_path: str | None = None

    @property
    def file_extension(self) -> str:
        """Lower-case extension of :attr:`file_name` (``""`` when absent)."""
        _, dot, extension = self.file_name.rpartition(".")
        return extension.lower() if dot else ""


@dataclass(frozen=True, slots=True)
class ReactionSpec:
    """One row of ``message_reactions``."""

    #: Seeded emoji value (must be a :class:`ReactionEmoji` member).
    emoji: str
    #: Username of the operator that placed it (``CRM`` source).
    operator_username: str | None = None
    #: Telegram user id, for a reaction mirrored from Telegram.
    tg_user_id: int | None = None

    @property
    def is_crm(self) -> bool:
        """``True`` when a CRM operator placed the reaction."""
        return self.operator_username is not None

    @property
    def source(self) -> str:
        """``CRM`` or ``TELEGRAM`` — matches ``MessageReaction.source``."""
        return REACTION_SOURCE_CRM if self.is_crm else REACTION_SOURCE_TELEGRAM


@dataclass(frozen=True, slots=True)
class QuickReplySpec:
    """One row of ``quick_replies`` (CONTRACTS_V5 5.4).

    ``shortcut`` is the natural key the seeder de-duplicates on, so re-running it
    never creates a second copy of a canned answer.
    """

    shortcut: str
    title: str
    body: str
    sort_order: int
    #: Grouping label; 5.1 defaults to ``general``.
    category: str = "general"
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class MediaMessageSpec:
    """A demo media message appended after the authored transcript of a chat.

    ``tg_msg_id`` is derived from the chat's *last* authored ``seq`` + ``offset``,
    so the demo messages continue the same numbering a real Telegram chat would
    have used, and the natural key ``(chat_id, tg_msg_id)`` stays stable across
    runs (which is what makes the seed idempotent without a new lookup column).
    """

    chat_key: str
    #: ``tg_msg_id = TG_MSG_ID_START + last_authored_seq + offset``.
    offset: int
    #: Minutes before "now" → always newer than the whole authored transcript.
    offset_minutes: int
    sender_type: str
    sender_name: str
    caption: str
    attachments: tuple[AttachmentSpec, ...]
    #: Username of the operator that sent it (outgoing media only).
    sender_operator_username: str | None = None
    #: Telegram user id of the client that sent it (incoming media only).
    sender_tg_id: int | None = None
    is_deleted_in_tg: bool = False
    deleted_after_minutes: int | None = None
    reply_to_offset: int | None = None

    @property
    def kind(self) -> str:
        """Declared media kind: the first attachment's kind (``PHOTO``/``DOCUMENT``)."""
        return self.attachments[0].kind if self.attachments else MessageKind.TEXT.value

    @property
    def is_outgoing(self) -> bool:
        """``True`` when an operator sent the media."""
        return self.sender_type == MessageSenderType.OPERATOR.value


@dataclass(frozen=True, slots=True)
class ChatSpec:
    """One row of ``group_chats``."""

    key: str
    tg_chat_id: int
    title: str
    username: str
    merchant_code: str
    #: Username of the assignee, or ``None`` to leave the chat unassigned.
    assigned_username: str | None
    unread_count: int
    #: ``last_message_at`` = anchor - ``last_offset_minutes``.
    last_offset_minutes: int
    #: ``created_at`` = anchor - ``created_offset_hours``.
    created_offset_hours: int
    primary_tx: str
    client_name: str


@dataclass(frozen=True, slots=True)
class MessageSpec:
    """One message that becomes a row of ``group_messages``.

    ``tg_msg_id`` is derived as ``TG_MSG_ID_START + seq`` so the sequence is
    stable across runs; ``reply_to_seq`` and ``incident_key`` are resolved to real
    database ids at insert time, which keeps the builder pure and testable.
    """

    chat_key: str
    seq: int
    #: ``created_at`` = anchor - ``offset_minutes``.
    offset_minutes: int
    sender_type: str
    sender_name: str
    text: str
    sender_operator_username: str | None = None
    sender_tg_id: int | None = None
    reply_to_seq: int | None = None
    phone: str | None = None
    is_deleted_in_tg: bool = False
    deleted_after_minutes: int | None = None
    #: Set on the operator reply that RESOLVED the incident.
    resolves_incident: str | None = None
    #: Set on the client message that OPENED the incident.
    triggers_incident: str | None = None
    delivery_status: str = DeliveryStatus.SENT.value
    #: Mirrors the mock admin panel status quoted in the text (BOT alerts only).
    tx_status: str | None = None

    @property
    def tg_msg_id(self) -> int:
        """Telegram message id of this row (sequential per chat, oldest first).

        ``seq`` is the position in the *chronological* spec list that
        :func:`build_messages` returns, so ``tg_msg_id`` grows with
        ``created_at`` exactly like a real Telegram chat.
        """
        return TG_MSG_ID_START + self.seq

    @property
    def is_outgoing(self) -> bool:
        """``True`` for operator replies (the API derives this from sender_type)."""
        return self.sender_type == MessageSenderType.OPERATOR.value


@dataclass(frozen=True, slots=True)
class IncidentSpec:
    """One row of ``sla_incidents``."""

    key: str
    chat_key: str
    plan: IncidentPlan
    assigned_username: str | None
    #: ``seq`` of the client message that started the clock.
    trigger_seq: int
    #: Resolved incidents: ``seq`` of the operator reply that closed it.
    resolve_seq: int | None = None
    response_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class AuditSpec:
    """One row of ``tx_lookup_audit``."""

    operator_username: str
    tx_id: str
    admin_panel: str
    found: bool
    response_status: str | None
    amount: Decimal | None
    lookup_ms: int
    offset_seconds: int


@dataclass(slots=True)
class SeedStats:
    """Counters reported by :func:`seed`."""

    operators_created: int = 0
    operators_skipped: int = 0
    chats_created: int = 0
    chats_skipped: int = 0
    messages_created: int = 0
    messages_skipped: int = 0
    incidents_created: int = 0
    incidents_skipped: int = 0
    audits_created: int = 0
    audits_skipped: int = 0
    #: ``media_attachments`` rows (demo media of CONTRACTS_V2.md §2.1).
    attachments_created: int = 0
    attachments_skipped: int = 0
    #: ``message_reactions`` rows (CONTRACTS_V2.md §2.2).
    reactions_created: int = 0
    reactions_skipped: int = 0
    #: ``quick_replies`` rows (CONTRACTS_V5 5.4).
    quick_replies_created: int = 0
    quick_replies_skipped: int = 0
    history_skipped_chats: int = 0
    reset: bool = False
    history: bool = True
    tables_dropped: list[str] = field(default_factory=list)
    sqlite_compat: bool = False

    def as_counts(self) -> dict[str, int]:
        """Return only the integer counters (what ``seed()`` returns)."""
        return {
            "operators_created": self.operators_created,
            "operators_skipped": self.operators_skipped,
            "chats_created": self.chats_created,
            "chats_skipped": self.chats_skipped,
            "messages_created": self.messages_created,
            "messages_skipped": self.messages_skipped,
            "incidents_created": self.incidents_created,
            "incidents_skipped": self.incidents_skipped,
            "audits_created": self.audits_created,
            "audits_skipped": self.audits_skipped,
            "attachments_created": self.attachments_created,
            "attachments_skipped": self.attachments_skipped,
            "reactions_created": self.reactions_created,
            "reactions_skipped": self.reactions_skipped,
            "quick_replies_created": self.quick_replies_created,
            "quick_replies_skipped": self.quick_replies_skipped,
        }


# --------------------------------------------------------------------------- #
# Chat catalogue
# --------------------------------------------------------------------------- #

CHAT_SPECS: Final[tuple[ChatSpec, ...]] = (
    ChatSpec(
        key="alpha",
        tg_chat_id=-1_001_234_567_890,
        title="Merchant Alpha — Payouts",
        username="merchant_alpha_support",
        merchant_code="MRC-ALPHA",
        assigned_username="op1",
        unread_count=3,
        last_offset_minutes=6,
        created_offset_hours=720,
        primary_tx="TX-4412-DD",
        client_name="Jane Doe @jane_mrc",
    ),
    ChatSpec(
        key="beta",
        tg_chat_id=-1_001_234_567_891,
        title="Merchant Beta — Settlements",
        username="merchant_beta_support",
        merchant_code="MRC-BETA",
        assigned_username="op2",
        unread_count=5,
        last_offset_minutes=1,
        created_offset_hours=680,
        primary_tx="TX-7781-AC",
        client_name="Maria Santos",
    ),
    ChatSpec(
        key="gamma",
        tg_chat_id=-1_001_234_567_892,
        title="Merchant Gamma — Chargebacks",
        username="merchant_gamma_support",
        merchant_code="MRC-GAMMA",
        assigned_username="op3",
        unread_count=2,
        last_offset_minutes=28,
        created_offset_hours=640,
        primary_tx="TX-9931-ZQ",
        client_name="Liu Wei",
    ),
    ChatSpec(
        key="delta",
        tg_chat_id=-1_001_234_567_893,
        title="Merchant Delta — KYC",
        username="merchant_delta_support",
        merchant_code="MRC-DELTA",
        assigned_username="op5",
        unread_count=1,
        last_offset_minutes=54,
        created_offset_hours=600,
        primary_tx="TX-1102-BE",
        client_name="Ahmed Karim",
    ),
    ChatSpec(
        key="epsilon",
        tg_chat_id=-1_001_234_567_894,
        title="Merchant Epsilon — Refunds",
        username="merchant_epsilon_support",
        merchant_code="MRC-EPSILON",
        assigned_username=None,
        unread_count=0,
        last_offset_minutes=95,
        created_offset_hours=540,
        primary_tx="TX-9931-ZQ",
        client_name="Elena Petrova",
    ),
    ChatSpec(
        key="zeta",
        tg_chat_id=-1_001_234_567_895,
        title="Merchant Zeta — Integrations / API",
        username="merchant_zeta_support",
        merchant_code="MRC-ZETA",
        assigned_username=None,
        unread_count=4,
        last_offset_minutes=133,
        created_offset_hours=480,
        primary_tx="TX-7781-AC",
        client_name="Tom Okafor",
    ),
)

CHAT_BY_KEY: Final[dict[str, ChatSpec]] = {spec.key: spec for spec in CHAT_SPECS}

#: Raw Telegram user ids for the seeded client personas (never reused).
CLIENT_TG_IDS: Final[dict[str, int]] = {
    "Jane Doe @jane_mrc": 771_234,
    "Maria Santos": 884_512,
    "Liu Wei": 991_007,
    "Ahmed Karim": 663_421,
    "Elena Petrova": 552_318,
    "Tom Okafor": 437_905,
}

BOT_TG_IDS: Final[dict[str, int]] = {
    "AcmePayout Bot": 5_123_456_789,
    "GatewayMonitor": 5_567_801_234,
}

#: ``tx_id -> (amount, currency, provider)`` for the alerts quoting a transaction.
#: Mirrors ``MockAdminMixin.CURATED_DATASET`` so the transcript and the transaction
#: sidebar never disagree.
BOT_AMOUNTS: Final[dict[str, tuple[str, str, str]]] = {
    "TX-4412-DD": ("1500.00", "USDT", "AcmePayout"),
    "TX-7781-AC": ("240.50", "EUR", "SwiftBank"),
    "TX-1102-BE": ("89.99", "USD", "PayGate"),
    "TX-9931-ZQ": ("3120.00", "USDT", "CryptoBridge"),
}

#: Which transaction a chat's automated alerts talk about, and with which status.
CHAT_BY_TX_STATUS: Final[dict[str, tuple[str, str]]] = {
    "alpha": ("TX-4412-DD", "PROCESSING"),
    "beta": ("TX-1102-BE", "PENDING"),
    "gamma": ("TX-9931-ZQ", "REFUNDED"),
    "delta": ("TX-1102-BE", "PROCESSING"),
    "epsilon": ("TX-9931-ZQ", "REFUNDED"),
    "zeta": ("TX-7781-AC", "COMPLETED"),
}

#: Which operators reply in each chat. Index 0 owns the chat, index 1 helps out.
CHAT_OPERATOR_SLOTS: Final[dict[str, tuple[str, str]]] = {
    "alpha": ("op1", "op2"),
    "beta": ("op2", "op1"),
    "gamma": ("op3", "op1"),
    "delta": ("op5", "op1"),
    "epsilon": ("op4", "op1"),
    "zeta": ("op6", "op1"),
}

# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #


def _shift_for_index(index: int) -> str:
    """op1..op4 ONLINE, op5..op6 PAUSE, op7..op10 OFFLINE (pattern repeats)."""
    cycle = (index - 1) % 10
    if cycle < 4:
        return ShiftStatus.ONLINE.value
    if cycle < 6:
        return ShiftStatus.PAUSE.value
    return ShiftStatus.OFFLINE.value


def _operator_display_name(index: int) -> str:
    """``1 -> "Operator One"``; numbers above 20 fall back to ``Operator N``."""
    if 1 <= index <= len(_NUMBER_WORDS):
        return f"Operator {_NUMBER_WORDS[index - 1]}"
    return f"Operator {index}"


def _avatar_color_for(username: str) -> str:
    """Stable, well-distributed avatar colour for ``username`` (never random)."""
    if username == settings.backend_supervisor_username:
        return SUPERVISOR_COLOR
    digits = username[2:]
    if username.startswith("op") and digits.isdigit():
        index = int(digits) - 1
    else:
        index = sum(ord(char) for char in username)
    return AVATAR_COLORS[index % len(AVATAR_COLORS)]


def _max_chats_for(index: int) -> int | None:
    """``op1..op4`` may hold 6 chats at once; the rest are uncapped (``None``)."""
    return 6 if index <= 4 else None


def supervisor_username() -> str:
    """Username the ``.env`` supervisor account is seeded under.

    ``settings.backend_supervisor_username`` defaults to ``"admin"``, which is now
    the *administrator* account. On a collision the supervisor is seeded as
    ``"supervisor"`` so both roles keep a working login; every other configured
    value is used verbatim.
    """
    configured = str(getattr(settings, "backend_supervisor_username", "") or "").strip()
    if not configured or configured == ADMIN_USERNAME:
        return SUPERVISOR_USERNAME
    return configured


def build_admin_operator() -> OperatorSpec:
    """Return the seeded administrator (CONTRACTS_V2.md §3.3).

    ``admin`` / ``admin123`` with ``display_name="Администратор"`` and the
    ``ADMIN`` role: a separate login form in the UI leads here, and only this role
    may create operators, change roles or manage Telegram accounts.
    """
    return OperatorSpec(
        username=ADMIN_USERNAME,
        display_name=ADMIN_DISPLAY_NAME,
        role=ROLE_ADMIN,
        shift_status=ShiftStatus.OFFLINE.value,
        is_online_ws=False,
        sla_target_seconds=SLA_TARGET_SECONDS,
        last_seen_offset=0,
        is_active=True,
        max_chats=None,
        can_delete_messages=True,
        can_export=True,
        avatar_color=ADMIN_COLOR,
        password_kind="admin",
    )


def build_operators(count: int = DEFAULT_OPERATOR_COUNT) -> list[OperatorSpec]:
    """Return the administrator, the supervisor and ``op1..opN``.

    The admin comes first so it is the lowest id; the supervisor follows, then
    ``op1..op4`` are ONLINE with an open WebSocket (they look like agents who are
    actively working), ``op5..op6`` are on PAUSE with a recent ``last_seen_at``
    and ``op7..op10`` are OFFLINE with a stale one, so the roster and the
    SLA/assignment views have every shift state represented.

    Accounts are ordered admin → supervisor → operators because
    :func:`_upsert_operators` assigns password hashes per ``password_kind`` and
    the admin must never inherit the operator password.
    """
    if not 1 <= count <= MAX_OPERATOR_COUNT:
        raise ValueError(
            f"--operators must be between 1 and {MAX_OPERATOR_COUNT}, got {count!r}"
        )
    admin = build_admin_operator()
    sup_name = supervisor_username()
    if sup_name == ADMIN_USERNAME:  # pragma: no cover - guarded by supervisor_username()
        raise ValueError("supervisor and administrator usernames would collide")
    specs: list[OperatorSpec] = [
        admin,
        OperatorSpec(
            username=sup_name,
            display_name="Supervisor",
            role=ROLE_SUPERVISOR,
            shift_status=ShiftStatus.ONLINE.value,
            is_online_ws=True,
            sla_target_seconds=SLA_TARGET_SECONDS,
            last_seen_offset=0,
            is_supervisor=True,
            is_active=True,
            max_chats=None,
            can_delete_messages=True,
            can_export=True,
            avatar_color=SUPERVISOR_COLOR,
            password_kind="supervisor",
        ),
    ]
    for index in range(1, count + 1):
        shift = _shift_for_index(index)
        if shift == ShiftStatus.ONLINE.value:
            offset = 5
        elif shift == ShiftStatus.PAUSE.value:
            offset = 7 * 60
        else:
            offset = 2 * 60 + 15
        username = f"op{index}"
        specs.append(
            OperatorSpec(
                username=username,
                display_name=_operator_display_name(index),
                role=ROLE_OPERATOR,
                shift_status=shift,
                is_online_ws=shift == ShiftStatus.ONLINE.value,
                sla_target_seconds=SLA_TARGET_SECONDS,
                last_seen_offset=offset,
                is_active=True,
                max_chats=_max_chats_for(index),
                can_delete_messages=username in PRIVILEGED_OPERATOR_USERNAMES,
                can_export=username in PRIVILEGED_OPERATOR_USERNAMES,
                avatar_color=_avatar_color_for(username),
                password_kind="operator",
            )
        )
    return specs


def build_chats() -> list[ChatSpec]:
    """Return the six merchant group chats (4 assigned, 2 unassigned)."""
    return list(CHAT_SPECS)


# --------------------------------------------------------------------------- #
# Message scripts
# --------------------------------------------------------------------------- #
#
# Each line is ``TURN|payload``:
#
#   client|Name|text      a merchant-side participant
#   bot|Name|text         an automated alert (AcmePayout Bot / GatewayMonitor)
#   o#|text               an operator reply; ``o#`` alternates owner/helper
#   deleted|TYPE|...      same payload as TYPE, flagged as deleted in Telegram
#
# ``{tx}`` expands to the chat's primary transaction id. A reply always quotes
# the newest client/bot turn that precedes it.

SCRIPT_ALPHA: Final[tuple[str, ...]] = (
    "bot|AcmePayout Bot|Transaction: {tx} | Provider: AcmePayout | Amount: 1500.00 USDT | Status: FAILED | Reason: Insufficient provider balance",
    "client|Jane Doe @jane_mrc|Payout <code>{tx}</code> flipped to FAILED and nothing reached our wallet. Callback +1 555 0100 if you need to reach me off-thread.",
    "client|Jane Doe @jane_mrc|Our books close at 21:00 UTC. Is there a manual retry, or do we tell the merchants to wait until tomorrow?",
    "o#|I can see the failed attempt on the provider side. Triggering a manual retry now and I will confirm the new reference here.",
    "client|Jane Doe @jane_mrc|Thanks. Also, line #4412 appears twice in our statement — we are not paying the second line.",
    "o#|That duplicate is a display artefact of the retry: the provider authorised twice but only settled once. I voided the second line.",
    "bot|AcmePayout Bot|Transaction: TX-7781-AC | Provider: AcmePayout | Amount: 1200.00 USDT | Status: COMPLETED | Reason: none",
    "o#|TX-7781-AC completed and the retry on {tx} is now PROCESSING with the provider.",
    "deleted|client|Jane Doe @jane_mrc|Honestly this is unacceptable. Refund us everything from today or we move the whole volume tonight.",
    "client|Jane Doe @jane_mrc|Confirmed on our dashboard, both lines look right now. Please send the statement note so accounting can sign off.",
    "o#|Statement note sent to finance@merchant-alpha.com and copied to you, including the voided authorisation reference.",
    "client|Jane Doe @jane_mrc|Got it, books are closed. Thanks for handling this so quickly today.",
)

SCRIPT_BETA: Final[tuple[str, ...]] = (
    "client|Maria Santos|Good morning! Who is covering settlements today? We need the 30 May file signed off before noon.",
    "o#|Morning Maria, Operator Two here — I am on settlements until 20:00 UTC and can release that file myself.",
    "client|Maria Santos|Perfect. Batch for 30 May, reference <code>{tx}</code>, is still PENDING in our dashboard but already booked internally.",
    "bot|GatewayMonitor|Settlement batch 2024-05-30: PENDING | amount 84,000.00 USDT | provider AcmePayout | received 31 May at 08:12",
    "client|Maria Santos|That is 84k hedged against Friday payroll. Our CFO wants an ETA every hour until it lands.",
    "o#|Pulling the settlement ledger now. An internal batch id would shorten the trace, otherwise the provider reference is enough.",
    "client|Maria Santos|Received. One more: TX-1102-BE has been PROCESSING since yesterday. Reach me on +44 7700 900123 if it stalls.",
    "o#|Found the 30 May batch: held by an AML review flag, cleared 30 minutes ago, value date today, ETA two hours.",
    "deleted|client|Maria Santos|If this is not settled today we move our volume to another acquirer. We cannot pay payroll like this.",
    "deleted|o#|Understood — if it has not landed by 16:00 UTC I escalate to the provider network on the recorded call.",
    "client|Maria Santos|Good. Please confirm once received and send the advice to finance@merchant-beta.com.",
    "o#|TX-1102-BE is inside the normal window and will move with the batch. I will ping you either way.",
    "client|Maria Santos|Great, that works. Thanks for being available this early.",
)

SCRIPT_GAMMA: Final[tuple[str, ...]] = (
    "client|Liu Wei|Hi, a chargeback was opened this morning on {tx}. What is our evidence deadline?",
    "o#|You have 7 calendar days. Upload the signed manifest, courier photos and any customer chat and I will file it.",
    "bot|AcmePayout Bot|Transaction: TX-9931-ZQ | Provider: CryptoBridge | Amount: 3120.00 USDT | Status: REFUNDED | Reason: chargeback upheld by issuer",
    "client|Liu Wei|The goods were delivered and signed for — we have photos and the courier manifest. We want to contest it.",
    "client|Liu Wei|Unrelated, but TX-7781-AC completed today. Was that the retry from last week?",
    "o#|Yes, same payout, retried successfully. Evidence for {tx} is filed and the issuer has 30 days to rule.",
    "client|Liu Wei|One thing worries me: TX-0000-XX has also disappeared from our payout list. Who took it?",
    "o#|TX-0000-XX is not a real reference — it is the example id printed in the merchant portal footer. Nothing is missing.",
    "deleted|client|Liu Wei|If you refund without our consent I will escalate this to the regulator and pull the integration.",
    "client|Liu Wei|Ah, that explains it. Thanks for checking — please keep us posted on the chargeback ruling.",
    "o#|Will do. I have set a reminder for the ruling date and will reopen this thread the moment the issuer responds.",
)

SCRIPT_DELTA: Final[tuple[str, ...]] = (
    "client|Ahmed Karim|Hello, we uploaded our company documents a week ago and the account is still limited.",
    "o#|I picked the file up, Ahmed. The company extract has a visible stamp mismatch — can you send a clean PDF?",
    "client|Ahmed Karim|Sure. Do you also need the director's passport again? Ours expires next month.",
    "bot|GatewayMonitor|Compliance queue: 14 documents pending review, oldest 6 days 4 hours, merchant MRC-DELTA flagged for manual review.",
    "client|Ahmed Karim|Uploaded as PDF. Also reachable on +971 50 123 4567 if anything else is missing.",
    "o#|No, the passport on file is still valid — expiry within 30 days is fine for onboarding. Only the extract needed replacing.",
    "client|Ahmed Karim|Any update? Our payouts are capped at 5,000 per day until KYC clears and it is hurting us.",
    "o#|Documents approved in review just now. The daily cap lifts automatically within 15 minutes.",
    "bot|AcmePayout Bot|Transaction: TX-1102-BE | Provider: AcmePayout | Amount: 89.99 USD | Status: PROCESSING | Reason: awaiting provider settlement window",
    "deleted|client|Ahmed Karim|Still capped on our side. If it is not lifted today we reconsider the partnership entirely.",
    "o#|Cap is lifted and TX-1102-BE is moving again. Please confirm on your dashboard and shout if it stalls.",
    "client|Ahmed Karim|Confirmed, we are unblocked. Thank you!",
)

SCRIPT_EPSILON: Final[tuple[str, ...]] = (
    "client|Elena Petrova|Our auditor asked for every refund booked in April, with the original references.",
    "o#|Sending the April refund report now: 41 records, 9,812.40 USDT total, one line per provider reference.",
    "client|Elena Petrova|Received. One record looks duplicated — {tx} appears twice with different timestamps.",
    "o#|The second line was rejected by the provider, so only one refund left your balance. Corrected report re-sent as -v2.",
    "bot|AcmePayout Bot|Transaction: TX-9931-ZQ | Provider: CryptoBridge | Amount: 3120.00 USDT | Status: REFUNDED | Reason: chargeback upheld by issuer",
    "client|Elena Petrova|Also TX-9931-ZQ shows REFUNDED but we never approved that refund. Please correct the report before we sign it.",
    "o#|TX-9931-ZQ is a genuine refund that was later disputed by the cardholder. The chargeback is attached to the same record.",
    "deleted|client|Elena Petrova|If the report is wrong I will ask for the whole month of refunds to be reversed back to us instead.",
    "client|Elena Petrova|That matches our ledger now. Please send the signed reconciliation summary to audit@merchant-epsilon.com.",
    "o#|Sent, with the chargeback note attached. Ping me if your auditor wants the raw provider lines as well.",
    "client|Elena Petrova|Will do. Thanks for the clean turnaround on this.",
)

SCRIPT_ZETA: Final[tuple[str, ...]] = (
    "client|Tom Okafor|Integration question: our payout webhook stopped receiving events at 03:00 UTC. Did something change on your side?",
    "bot|GatewayMonitor|API health: callback delivery 92.4% over the last 24h, 37 events timed out to mrc-zeta.internal.",
    "client|Tom Okafor|We rotated the certificate last night, that is probably it. Testing again with {tx} now.",
    "o#|That is a TLS handshake timeout on your endpoint, not an API change. We retry 5 times over 30 minutes, then drop the event.",
    "client|Tom Okafor|Still nothing on our side. Our ops channel is +44 7700 900456 if it helps to align on timing.",
    "o#|Event for TX-7781-AC was delivered and your endpoint returned 200. The certificate fix worked — signature verified.",
    "bot|AcmePayout Bot|Transaction: TX-7781-AC | Provider: AcmePayout | Amount: 1200.00 USDT | Status: COMPLETED | Reason: none",
    "o#|On the key rotation you flagged: the audit log shows it came from your own account id on 29 May, not from us.",
    "client|Tom Okafor|That was our old contractor. Can you issue a new key and revoke the previous one?",
    "o#|New key issued, previous credential revoked, and the rotation runbook was sent to your integration team.",
    "deleted|client|Tom Okafor|Also, someone rotated our API key without telling us. If that was you, treat this as a security incident.",
    "client|Tom Okafor|Got it, everything is green in staging. Thanks for the solid support today.",
)

CHAT_SCRIPTS: Final[dict[str, tuple[str, ...]]] = {
    "alpha": SCRIPT_ALPHA,
    "beta": SCRIPT_BETA,
    "gamma": SCRIPT_GAMMA,
    "delta": SCRIPT_DELTA,
    "epsilon": SCRIPT_EPSILON,
    "zeta": SCRIPT_ZETA,
}

# --------------------------------------------------------------------------- #
# Demo media + reactions (CONTRACTS_V2.md §2.1 / §2.2)
# --------------------------------------------------------------------------- #
#
# Four media messages, one per every other chat, so the UI has something to
# render on the two busiest transcripts and the two stuck ones. They are appended
# *after* the authored dialogue (newest in the chat) but are NOT incident
# openers: a photo must never start an SLA clock.


def media_kind_of(file_name: str, mime_type: str) -> str:
    """Map a file name + MIME type onto a :class:`MessageKind` value.

    The single place that derives a media kind from a file, shared by the seeder
    and (by contract) the media service, so the same file never ends up as
    ``PHOTO`` in one row and ``DOCUMENT`` in another.

    Order matters: a ``.webp`` animation is a ``STICKER`` before the generic
    ``image/*`` rule can call it a ``PHOTO``, and a ``.ogg`` voice note is a
    ``VOICE`` before ``audio/*`` makes it an ``AUDIO``.
    """
    extension = file_name.rpartition(".")[2].lower()
    lowered = mime_type.lower()
    if extension == "webp" and lowered in ("image/webp", "application/x-tgsticker"):
        return MessageKind.STICKER.value
    if extension in ("ogg", "opus") or lowered in ("audio/ogg", "audio/opus"):
        return MessageKind.VOICE.value
    if lowered.startswith("image/"):
        return MessageKind.PHOTO.value
    if lowered.startswith("video/"):
        return MessageKind.VIDEO.value
    if lowered.startswith("audio/"):
        return MessageKind.AUDIO.value
    if extension in ("pdf", "csv", "doc", "docx", "xls", "xlsx", "txt", "zip"):
        return MessageKind.DOCUMENT.value
    return MessageKind.OTHER.value


def _storage_dir(chat_key: str, message_offset: int) -> str:
    """Relative media folder of one demo message: ``chats/{tg_chat_id}/{tg_msg_id}``.

    ``tg_chat_id`` is the *Telegram* id from :data:`CHAT_SPECS` (negative for
    supergroups), so the seeded path is exactly what the worker writes when it
    downloads the same message (CONTRACTS_V2.md §5).
    """
    chat = CHAT_BY_KEY[chat_key]
    return f"chats/{chat.tg_chat_id}/{TG_MSG_ID_START + message_offset}"


def _attachment(
    chat_key: str,
    message_offset: int,
    file_name: str,
    mime_type: str,
    size_bytes: int,
    *,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: int | None = None,
    with_thumbnail: bool = False,
) -> AttachmentSpec:
    """Build one :class:`AttachmentSpec` with contract-compliant paths.

    ``storage_path``/``thumbnail_path`` are always relative to
    ``settings.media_root`` and use POSIX separators.
    """
    folder = _storage_dir(chat_key, message_offset)
    if with_thumbnail:
        # ``receipt.png`` -> ``thumb_receipt.jpg``: the preview is always a JPEG,
        # and the original extension is stripped so it is not doubled.
        stem = file_name.rpartition(".")[0] or file_name
        thumbnail_path = f"{folder}/thumb_{stem}.jpg"
    else:
        thumbnail_path = None
    return AttachmentSpec(
        kind=media_kind_of(file_name, mime_type),
        file_name=file_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        storage_path=f"{folder}/{file_name}",
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        tg_file_id=f"seed/{chat_key}/{message_offset}/{file_name}",
        thumbnail_path=thumbnail_path,
    )


def _demo_media() -> tuple[MediaMessageSpec, ...]:
    """Return the four demo media messages, built once paths are known."""
    return (
        MediaMessageSpec(
            chat_key="alpha",
            offset=1,
            offset_minutes=4,
            sender_type=MessageSenderType.CLIENT.value,
            sender_name="Jane Doe @jane_mrc",
            caption=(
                "Скриншот кабинета провайдера: вторая авторизация по TX-4412-DD "
                "висит со статусом FAILED."
            ),
            sender_tg_id=CLIENT_TG_IDS["Jane Doe @jane_mrc"],
            attachments=(
                _attachment(
                    "alpha",
                    1,
                    "receipt-4412.png",
                    "image/png",
                    48_211,
                    width=800,
                    height=600,
                    with_thumbnail=True,
                ),
            ),
        ),
        MediaMessageSpec(
            chat_key="alpha",
            offset=2,
            offset_minutes=2,
            sender_type=MessageSenderType.OPERATOR.value,
            sender_name=_operator_display_name(1),
            sender_operator_username="op1",
            caption="Сверка за 30 мая: обе строки, включая аннулированную авторизацию.",
            reply_to_offset=1,
            attachments=(
                _attachment(
                    "alpha",
                    2,
                    "statement-30-may.csv",
                    "text/csv",
                    7_844,
                ),
            ),
        ),
        MediaMessageSpec(
            chat_key="beta",
            offset=1,
            offset_minutes=8,
            sender_type=MessageSenderType.OPERATOR.value,
            sender_name=_operator_display_name(2),
            sender_operator_username="op2",
            caption=(
                "Подтверждение провайдера по батчу расчётов 2024-05-30, файл в PDF."
            ),
            attachments=(
                _attachment(
                    "beta",
                    1,
                    "settlement-2024-05-30.pdf",
                    "application/pdf",
                    182_664,
                ),
            ),
        ),
        MediaMessageSpec(
            chat_key="delta",
            offset=1,
            offset_minutes=11,
            sender_type=MessageSenderType.CLIENT.value,
            sender_name="Ahmed Karim",
            sender_tg_id=CLIENT_TG_IDS["Ahmed Karim"],
            caption="Прикладываю расписку курьера для повторной проверки KYC.",
            is_deleted_in_tg=True,
            deleted_after_minutes=3,
            attachments=(
                _attachment(
                    "delta",
                    1,
                    "courier-receipt.jpg",
                    "image/jpeg",
                    231_902,
                    width=1200,
                    height=1600,
                    with_thumbnail=True,
                ),
                _attachment(
                    "delta",
                    1,
                    "kyc-cover-letter.pdf",
                    "application/pdf",
                    41_318,
                ),
            ),
        ),
    )


DEMO_MEDIA: Final[tuple[MediaMessageSpec, ...]] = _demo_media()

#: ``(chat_key, media offset, reaction)`` — 13 reactions across the demo media and
#: the two most recent authored messages of every affected chat, mixed ``CRM`` and
#: ``TELEGRAM`` so the counter badges, the "моя реакция" highlight and the
#: Telegram-sourced bubbles all have data on a fresh database.
REACTION_PLAN: Final[tuple[tuple[str, int, tuple[ReactionSpec, ...]], ...]] = (
    ("alpha", 1, (
        ReactionSpec(ReactionEmoji.THUMBS_UP.value, operator_username="op1"),
        ReactionSpec(ReactionEmoji.FIRE.value, operator_username="op2"),
        ReactionSpec(ReactionEmoji.FIRE.value, tg_user_id=CLIENT_TG_IDS["Jane Doe @jane_mrc"]),
    )),
    ("alpha", 2, (
        ReactionSpec(ReactionEmoji.THUMBS_UP.value, operator_username="op2"),
        ReactionSpec(ReactionEmoji.CLAP.value, operator_username="op3"),
    )),
    ("beta", 1, (
        ReactionSpec(ReactionEmoji.THUMBS_UP.value, operator_username="op1"),
        ReactionSpec(ReactionEmoji.PRAY.value, tg_user_id=CLIENT_TG_IDS["Maria Santos"]),
    )),
    ("delta", 1, (
        ReactionSpec(ReactionEmoji.EYES.value, operator_username="op1"),
        ReactionSpec(ReactionEmoji.HEART.value, tg_user_id=CLIENT_TG_IDS["Ahmed Karim"]),
    )),
    # Authored (non-media) messages: the newest operator reply of alpha and beta.
    ("alpha", -2, (
        ReactionSpec(ReactionEmoji.HEART.value, operator_username="op2"),
        ReactionSpec(ReactionEmoji.PRAY.value, operator_username="op3"),
    )),
    ("beta", -2, (
        ReactionSpec(ReactionEmoji.CLAP.value, operator_username="op1"),
    )),
)

# --------------------------------------------------------------------------- #
# SLA incidents and audit rows
# --------------------------------------------------------------------------- #

#: Three live incidents, one per shape the UI must render:
#: ``due_soon`` (~4 min left), ``burning`` (< 60 s left) and ``overdue``.
LIVE_INCIDENTS: Final[tuple[IncidentSpec, ...]] = (
    IncidentSpec(key="live-alpha", chat_key="alpha", plan="due_soon",
                 assigned_username="op1", trigger_seq=11),
    IncidentSpec(key="live-beta", chat_key="beta", plan="burning",
                 assigned_username="op2", trigger_seq=11),
    IncidentSpec(key="live-gamma", chat_key="gamma", plan="overdue",
                 assigned_username=None, trigger_seq=9),
)

#: ``(chat, trigger_seq, response_seconds)`` for the 20 historical incidents.
#: ``trigger_seq`` is the absolute index of the *opener* in the merged dialogue;
#: the closer is the next operator reply (see :func:`_placement_for`), so every
#: incident opens on a real client/bot message and closes on a real reply.
#: Response times are mostly inside the 300 s target with a few breaches, which
#: lands the seeded SLA percentage around 90%.
HISTORICAL_INCIDENT_PLAN: Final[tuple[tuple[str, int, int], ...]] = (
    ("delta", 1, 62),
    ("zeta", 2, 118),
    ("alpha", 1, 197),
    ("beta", 1, 244),
    ("epsilon", 3, 288),
    ("gamma", 1, 41),
    ("alpha", 6, 155),
    ("beta", 7, 233),
    ("delta", 6, 276),
    ("zeta", 6, 94),
    ("epsilon", 7, 312),
    ("gamma", 6, 168),
    ("alpha", 9, 331),
    ("zeta", 9, 222),
    ("delta", 12, 259),
    ("beta", 13, 49),
    ("epsilon", 11, 141),
    ("gamma", 12, 86),
    ("alpha", 12, 101),
    ("zeta", 12, 189),
)

#: Operators that closed the historical incidents, in plan order. ``op1`` has the
#: most so ``GET /api/sla/stats/me`` is never empty for the default login.
HISTORICAL_ASSIGNEES: Final[tuple[str, ...]] = (
    "op1", "op2", "op1", "op3", "op4", "op1", "op2", "op3", "op1", "op5",
    "op4", "op1", "op2", "op3", "op1", "op4", "op5", "op1", "op2", "op3",
)

#: Opener/reply text per conversation, cycled for merged historical incidents.
HISTORICAL_DIALOGUE: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "alpha": (
        ("Our payout statement is off by 120 USDT again this week, can someone check the ledger?",
         "Reconciled the ledger, the 120 USDT was a fee booked twice. Corrected and re-issued."),
        ("Is tomorrow's payout batch included in the weekend run or does it slip to Monday?",
         "It is included — the weekend cutoff is 14:00 UTC, anything earlier goes out on Saturday."),
        ("We need a VAT invoice for April, accounting is chasing us.",
         "Invoice generated, you will find it in the finance portal and I copied your accountant."),
    ),
    "beta": (
        ("Our settlement report does not match the payout screen by 3 transactions.",
         "Found the gap, three records were still in the provider's cut-off window. Report re-issued."),
        ("Can you confirm the fee tier we are on for EUR settlements?",
         "You are on the 0.9% EUR tier, unchanged since March. Sending the tariff sheet."),
    ),
    "gamma": (
        ("Two chargebacks were opened yesterday, do we need to upload evidence today?",
         "Uploaded evidence for both and filed within the deadline — you have 7 days, no rush."),
        ("Will the chargeback fee be refunded if we win the dispute?",
         "Yes, the fee is reversed automatically when the issuer rules in your favour."),
    ),
    "delta": (
        ("We want to add a second director to the account, what documents do you need?",
         "Send the new director's passport and proof of address and I will start the review."),
        ("Is the KYC refresh annual or biennial for us?",
         "Biennial for your risk band — next refresh is due in 2026, I put a reminder on the file."),
    ),
    "epsilon": (
        ("A customer asked for a refund we already processed, can you confirm it went out?",
         "Confirmed, the refund left your balance on the provider side. Reference sent to you."),
    ),
    "zeta": (
        ("The sandbox API returns 401 for our new key, did the rotation propagate?",
         "It had not propagated to sandbox yet, I forced the sync. Try again now."),
        ("Do you have an OpenAPI spec we can generate a client from?",
         "Published at /api/openapi.json and I pinned the version for you."),
    ),
}

#: ``TxLookupAudit`` rows so the supervisor audit panel is not empty.
#: Amounts and statuses mirror ``MockAdminMixin.CURATED_DATASET``.
AUDIT_SPECS: Final[tuple[AuditSpec, ...]] = (
    AuditSpec("op1", "TX-4412-DD", TxAdminPanel.ADMIN_ONE.value, True,
              TxStatus.FAILED.value, Decimal("1500.00"), 214, 35),
    AuditSpec("op1", "TX-4412-DD", TxAdminPanel.ADMIN_TWO.value, True,
              TxStatus.DECLINED.value, Decimal("1500.00"), 268, 33),
    AuditSpec("op2", "TX-7781-AC", TxAdminPanel.ADMIN_ONE.value, True,
              TxStatus.COMPLETED.value, Decimal("240.50"), 181, 22),
    AuditSpec("op2", "TX-1102-BE", TxAdminPanel.ADMIN_ONE.value, True,
              TxStatus.PROCESSING.value, Decimal("89.99"), 236, 12),
    AuditSpec("op3", "TX-9931-ZQ", TxAdminPanel.ADMIN_TWO.value, True,
              TxStatus.REFUNDED.value, Decimal("3120.00"), 172, 8),
    AuditSpec(settings.backend_supervisor_username, "TX-0000-XX",
              TxAdminPanel.ADMIN_ONE.value, False, None, None, 96, 4),
)


def build_incidents() -> list[IncidentSpec]:
    """Return the 3 live incidents followed by the 20 historical ones.

    ``trigger_seq``/``resolve_seq`` are absolute indices into the merged dialogue
    of each chat; :func:`build_messages` applies the same insertion rules, so the
    two stay in sync without duplicating the merge logic here.
    """
    incidents: list[IncidentSpec] = list(LIVE_INCIDENTS)
    if len(HISTORICAL_INCIDENT_PLAN) != len(HISTORICAL_ASSIGNEES):
        raise ValueError("historical incident plan and assignee list are out of sync")
    per_chat: dict[str, int] = {}
    for index, (chat_key, trigger_seq, seconds) in enumerate(
        HISTORICAL_INCIDENT_PLAN, start=1
    ):
        if seconds <= 0:
            raise ValueError(f"h{index:02d}: response_seconds must be positive")
        per_chat[chat_key] = per_chat.get(chat_key, 0) + 1
        incidents.append(
            IncidentSpec(
                key=f"h{index:02d}",
                chat_key=chat_key,
                plan="resolved",
                assigned_username=HISTORICAL_ASSIGNEES[index - 1],
                trigger_seq=trigger_seq,
                resolve_seq=_closer_for(chat_key, trigger_seq),
                response_seconds=seconds,
            )
        )
    return incidents


def _closer_for(chat_key: str, trigger_seq: int) -> int:
    """Absolute index of the operator reply that closes an incident opened at ``trigger_seq``.

    The historical openers are spliced into the authored dialogue, and an
    insertion shifts only the turns *after* it, so the mapping between authored
    and merged indices is not a single offset. The reliable derivation is to
    replay the merge (:func:`_merge_history` is the single source of truth) and
    take the first operator reply after the opener in the merged sequence.
    """
    openers = [
        seq
        for chat, seq, _ in HISTORICAL_INCIDENT_PLAN
        if chat == chat_key
    ]
    placeholders = [
        IncidentSpec(key="probe", chat_key=chat_key, plan="resolved",
                     assigned_username=None, trigger_seq=seq)
        for seq in openers
    ]
    merged = _merge_history(chat_key, _parse_script(chat_key), placeholders)
    for index in range(trigger_seq + 1, len(merged)):
        if merged[index].kind == "o#":
            return index
    raise ValueError(
        f"{chat_key}: opener at {trigger_seq} has no operator reply to close it"
    )


def build_audits() -> list[AuditSpec]:
    """Return the seeded ``tx_lookup_audit`` rows."""
    return list(AUDIT_SPECS)


def build_attachments(chat_key: str) -> list[AttachmentSpec]:
    """Return every attachment (flattened) of the demo media in ``chat_key``."""
    specs: list[AttachmentSpec] = []
    for media in DEMO_MEDIA:
        if media.chat_key == chat_key:
            specs.extend(media.attachments)
    return specs


def build_reactions(chat_key: str) -> list[tuple[int, ReactionSpec]]:
    """Return ``(media offset, reaction)`` pairs for ``chat_key``.

    A negative offset addresses an *authored* message counted from the end of the
    transcript (``-1`` = newest, ``-2`` = second newest); a positive offset
    addresses a demo media message by its :attr:`MediaMessageSpec.offset`.
    """
    pairs: list[tuple[int, ReactionSpec]] = []
    for plan_chat, offset, reactions in REACTION_PLAN:
        if plan_chat != chat_key:
            continue
        pairs.extend((offset, reaction) for reaction in reactions)
    return pairs


# --------------------------------------------------------------------------- #
# Message builder
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _RawTurn:
    """A dialogue line before it is merged into the final message sequence."""

    kind: Literal["client", "bot", "o#"]
    name: str
    text: str
    deleted: bool
    #: Incident this turn opens (client) or resolves (operator), if any.
    triggers: str | None = None
    resolves: str | None = None


def _parse_script(chat_key: str) -> list[_RawTurn]:
    """Turn the authored script of ``chat_key`` into typed turns."""
    script = CHAT_SCRIPTS.get(chat_key)
    if script is None:
        raise KeyError(f"unknown chat key {chat_key!r}")
    tx_id = CHAT_BY_KEY[chat_key].primary_tx
    turns: list[_RawTurn] = []
    for line in script:
        head, _, rest = line.partition("|")
        deleted = head == "deleted"
        if deleted:
            head, _, rest = rest.partition("|")
        if head in ("client", "bot"):
            name, _, text = rest.partition("|")
            turns.append(_RawTurn(head, name.strip(), text.strip().replace("{tx}", tx_id), deleted))
        elif head == "o#":
            turns.append(_RawTurn("o#", "", rest.strip().replace("{tx}", tx_id), deleted))
        else:  # pragma: no cover - scripts are authored, not generated
            raise ValueError(f"{chat_key}: unknown script turn {line!r}")
    return turns


def _dialogue_for(chat_key: str, index: int) -> tuple[str, str]:
    """Pick the ``index``-th (wrapping) historical opener/reply pair for a chat."""
    pairs = HISTORICAL_DIALOGUE[chat_key]
    return pairs[index % len(pairs)]


def _merge_history(
    chat_key: str, base: list[_RawTurn], incidents: list[IncidentSpec]
) -> list[_RawTurn]:
    """Insert the historical incident openers, in chronological order.

    ``IncidentSpec.trigger_seq`` is absolute, so once the dialogue is authored
    without history the openers are spliced in ascending trigger order - an
    insertion at index *i* shifts every later index by exactly one, which is what
    :func:`_placement_for` compensates for when it computes those indices.
    """
    chat_name = CHAT_BY_KEY[chat_key].client_name
    merged = list(base)
    inserted = 0
    for spec in sorted(incidents, key=lambda item: item.trigger_seq):
        opener, _ = _dialogue_for(chat_key, inserted)
        inserted += 1
        merged.insert(
            spec.trigger_seq,
            _RawTurn("client", chat_name, opener, False, triggers=spec.key),
        )
    return merged


def _bot_alert_for(chat_key: str, spec: IncidentSpec) -> str:
    """Build the automated alert that OPENS a historical incident.

    The status follows the conversation: Merchant Beta argues about a stuck
    settlement (``PENDING``), Merchant Gamma is chasing a refund (``REFUNDED``),
    Merchant Delta is blocked on KYC (``PROCESSING``) and so on.
    """
    tx_id = CHAT_BY_TX_STATUS.get(chat_key, (CHAT_BY_KEY[chat_key].primary_tx, "PROCESSING"))[0]
    status = CHAT_BY_TX_STATUS.get(chat_key, (tx_id, "PROCESSING"))[1]
    if chat_key == "delta":
        return (
            "Compliance queue: 14 documents pending review, merchant MRC-DELTA "
            "flagged for a manual second pass over the company extract."
        )
    amount, currency, provider = BOT_AMOUNTS.get(tx_id, ("0.00", "USDT", "AcmePayout"))
    return (
        f"Transaction: {tx_id} | Provider: {provider} | Amount: {amount} {currency} | "
        f"Status: {status} | Reason: provider update received"
    )


def build_messages(chat_key: str) -> list[MessageSpec]:
    """Build the full, chronological history of one chat.

    Timestamps descend backwards from ``last_message_at`` with 3-25 minute gaps
    (fixed seed), so the newest message exactly matches the chat's
    ``last_message_at`` and the oldest stays inside the last 48 hours.
    """
    chat = CHAT_BY_KEY.get(chat_key)
    if chat is None:
        raise KeyError(f"unknown chat key {chat_key!r}")

    incidents = [spec for spec in build_incidents() if spec.chat_key == chat_key]
    historical = [spec for spec in incidents if spec.plan == "resolved"]
    live_specs = [spec for spec in incidents if spec.plan != "resolved"]
    turns = _merge_history(chat_key, _parse_script(chat_key), historical)

    # A pending incident means the merchant is still waiting, so its opener must be
    # the newest message of the chat and nothing may follow it. The authored
    # dialogue keeps its length (the historical indices above are absolute), so the
    # final turn is reserved for that opener. A chat whose last turn already is an
    # operator reply needs that reply back: the incident it closed is re-pointed at
    # an earlier, still-unused operator turn.
    live_triggers: dict[int, str] = {}
    if live_specs:
        last = len(turns) - 1
        if turns[last].kind == "o#":
            used = {spec.resolve_seq for spec in historical}
            spare = [
                index
                for index, turn in enumerate(turns)
                if turn.kind == "o#"
                and index < last
                and index not in used
            ]
            if not spare:
                raise ValueError(
                    f"{chat_key}: no free operator turn to re-house the resolver "
                    f"of the final message"
                )
            recent_owner = max(
                (spec for spec in historical if spec.resolve_seq == last),
                key=lambda item: item.trigger_seq,
                default=None,
            )
            if recent_owner is not None:
                # The replacement must still sit *after* that incident's opener,
                # otherwise the reply would precede the question it answers.
                candidates = [
                    index for index in spare if index > recent_owner.trigger_seq
                ] or spare
                historical = [
                    replace(spec, resolve_seq=candidates[-1])
                    if spec.key == recent_owner.key
                    else spec
                    for spec in historical
                ]
        live_triggers[last] = live_specs[0].key

    # Attribution is index-driven: an incident's opener is stamped with
    # ``triggers_incident`` and its closing reply with ``resolves_incident``.
    # Deriving both from the spec (instead of from loop position) keeps the flags
    # correct no matter how the dialogue is merged.
    opens_at: dict[int, str] = {spec.trigger_seq: spec.key for spec in historical}
    closes_at: dict[int, str] = {
        spec.resolve_seq: spec.key
        for spec in historical
        if spec.resolve_seq is not None and spec.resolve_seq != spec.trigger_seq
    }
    # ``text`` is the *reply that closes* incident X and ``trigger_seq`` is the
    # client message that opened it.
    bot_openers: dict[int, str] = {}
    for spec in historical:
        if spec.trigger_seq >= 0 and turns[spec.trigger_seq].kind == "bot":
            bot_openers[spec.trigger_seq] = _bot_alert_for(chat_key, spec)
    opener_seqs = {spec.trigger_seq for spec in historical} | set(live_triggers)
    if live_triggers:
        # The repurposed final turn must read as an unanswered merchant message.
        last = len(turns) - 1
        turns[last] = _RawTurn(
            "client", CHAT_BY_KEY[chat_key].client_name, turns[last].text, False
        )
    owner, helper = CHAT_OPERATOR_SLOTS[chat_key]

    rng = random.Random(f"{RANDOM_SEED}:{chat_key}")
    total = len(turns)
    # Oldest-first: the gap to the next (newer) turn is seeded per step. The newest
    # turn lands either on ``last_message_at`` or, for a chat waiting on a live
    # SLA incident, exactly ``elapsed`` seconds ago so the countdown is real.
    if live_specs:
        elapsed_seconds = _LIVE_OFFSETS[live_specs[0].plan][0]
        newest_offset = max(1, -(-elapsed_seconds // 60))
    else:
        newest_offset = chat.last_offset_minutes
    gaps = [rng.randint(3, 25) for _ in range(max(total - 1, 0))]
    offsets_by_seq: dict[int, int] = {total - 1: newest_offset}
    for index in range(total - 2, -1, -1):
        # ``gaps[index]`` is the distance between turn ``index`` and ``index + 1``.
        offsets_by_seq[index] = offsets_by_seq[index + 1] + gaps[index]

    specs: list[MessageSpec] = []
    operator_turn = 0
    for seq, turn in enumerate(turns):
        # A client message that OPENS an incident cannot quote an earlier one:
        # the SLA clock must start fresh, otherwise the incident's timeline
        # would contradict the thread it lives in.
        opens_incident = seq in opener_seqs
        if turn.kind == "o#":
            operator_turn += 1
            is_owner = operator_turn % 2 == 1
            operator = owner if is_owner else helper
            spec = MessageSpec(
                chat_key=chat_key,
                seq=seq,
                offset_minutes=offsets_by_seq[seq],
                sender_type=MessageSenderType.OPERATOR.value,
                sender_name=_display_name_of(operator),
                text=turn.text,
                sender_operator_username=operator,
                reply_to_seq=seq - 1 if seq > 0 else None,
                delivery_status=DeliveryStatus.SENT.value,
            )
        elif turn.kind == "bot":
            bot_text = bot_openers.get(seq, turn.text)
            status = _detect_bot_status(bot_text)
            spec = MessageSpec(
                chat_key=chat_key,
                seq=seq,
                offset_minutes=offsets_by_seq[seq],
                sender_type=MessageSenderType.BOT.value,
                sender_name=turn.name,
                text=bot_text,
                sender_tg_id=BOT_TG_IDS.get(turn.name),
                is_deleted_in_tg=turn.deleted,
                deleted_after_minutes=rng.randint(1, 20) if turn.deleted else None,
                tx_status=status,
            )
        else:
            spec = MessageSpec(
                chat_key=chat_key,
                seq=seq,
                offset_minutes=offsets_by_seq[seq],
                sender_type=MessageSenderType.CLIENT.value,
                sender_name=turn.name,
                text=turn.text,
                sender_tg_id=CLIENT_TG_IDS.get(turn.name),
                phone=_detect_phone(turn.text),
                is_deleted_in_tg=turn.deleted,
                deleted_after_minutes=rng.randint(1, 20) if turn.deleted else None,
                # A plain client message quotes the turn before it; a client
                # message that opens an incident starts a fresh thread.
                reply_to_seq=None if opens_incident or seq == 0 else seq - 1,
            )
        specs.append(
            MessageSpec(
                chat_key=spec.chat_key,
                seq=spec.seq,
                offset_minutes=spec.offset_minutes,
                sender_type=spec.sender_type,
                sender_name=spec.sender_name,
                text=spec.text,
                sender_operator_username=spec.sender_operator_username,
                sender_tg_id=spec.sender_tg_id,
                reply_to_seq=spec.reply_to_seq,
                phone=spec.phone,
                is_deleted_in_tg=spec.is_deleted_in_tg,
                deleted_after_minutes=spec.deleted_after_minutes,
                resolves_incident=closes_at.get(seq),
                triggers_incident=opens_at.get(seq),
                delivery_status=spec.delivery_status,
                tx_status=spec.tx_status,
            )
        )
    _assert_history(chat_key, specs, live_triggers)
    return specs


def _display_name_of(username: str) -> str:
    """``"op3"`` -> ``"Operator Three"`` (no database round-trip needed)."""
    digits = username[2:]
    if username.startswith("op") and digits.isdigit():
        return _operator_display_name(int(digits))
    return username


def _detect_bot_status(text: str) -> str | None:
    """Extract the transaction status quoted by a BOT alert (``None`` otherwise).

    ``None`` means "no transaction quoted" (e.g. a health-check alert); a
    ``NOT_FOUND`` status is explicitly ``TxStatus.NOT_FOUND``.
    """
    match = re.search(r"Status:\s*([A-Za-z_]+)", text)
    if match is None:
        return None
    candidate = match.group(1).upper()
    try:
        return TxStatus(candidate).value
    except ValueError:
        return None


def _detect_phone(text: str) -> str | None:
    """Return the first phone number of ``text`` using the shared extractor.

    ``app.utils.extract_phone`` returns E.164 (``+15550100``), which loses the
    human formatting shown in the transcript; the seeded value therefore keeps
    the readable grouping, and only falls back to E.164 when no grouping is
    present.
    """
    normalised = extract_phone(text)
    if normalised is None:
        return None
    match = _PHONE_RE.search(text)
    if match is not None:
        grouped = " ".join(match.group(1).split())
        if grouped.startswith("+"):
            return grouped
    return normalised


def _assert_history(
    chat_key: str, specs: list[MessageSpec], live_triggers: dict[int, str]
) -> None:
    """Fail fast when an authored script breaks a seeding guarantee."""
    count = len(specs)
    if not 10 <= count <= 16:
        raise ValueError(f"{chat_key}: expected 10-16 messages, got {count}")
    if [spec.seq for spec in specs] != list(range(count)):
        raise ValueError(f"{chat_key}: seq values are not contiguous")
    if specs[0].offset_minutes > 48 * 60:
        raise ValueError(
            f"{chat_key}: oldest message is older than 48h ({specs[0].offset_minutes} min)"
        )
    for earlier, later in zip(specs, specs[1:]):
        # Specs are chronological while ``offset_minutes`` counts minutes *before
        # now*, so the offset must shrink from one message to the next.
        gap = earlier.offset_minutes - later.offset_minutes
        if not 3 <= gap <= 25:
            raise ValueError(f"{chat_key}: gap of {gap} min between seq "
                             f"{earlier.seq} and {later.seq}")
    if sum(1 for s in specs if s.reply_to_seq is not None) < 2:
        raise ValueError(f"{chat_key}: needs at least 2 messages with reply_to_id")
    if not any(s.is_deleted_in_tg for s in specs):
        raise ValueError(f"{chat_key}: needs at least 1 deleted message")
    for spec in specs:
        if spec.is_deleted_in_tg and (
            spec.deleted_after_minutes is None
            or not 1 <= spec.deleted_after_minutes <= 20
        ):
            raise ValueError(f"{chat_key}: deleted_at offset out of range at seq {spec.seq}")
        if spec.sender_type == MessageSenderType.OPERATOR.value:
            if not spec.sender_operator_username:
                raise ValueError(f"{chat_key}: operator message without operator")
            if spec.delivery_status != DeliveryStatus.SENT.value:
                raise ValueError(f"{chat_key}: operator message not marked sent")
        if spec.sender_type == MessageSenderType.CLIENT.value and not spec.text:
            raise ValueError(f"{chat_key}: empty client message at seq {spec.seq}")
    for seq, key in live_triggers.items():
        if seq >= count:
            raise ValueError(f"{chat_key}: live incident {key} points at missing seq {seq}")
        if specs[seq].sender_type != MessageSenderType.CLIENT.value:
            raise ValueError(f"{chat_key}: live incident {key} not opened by a client")


# --------------------------------------------------------------------------- #
# Schema preparation (reset guard + SQLite shim)
# --------------------------------------------------------------------------- #


def _is_production() -> bool:
    """``True`` when the configured environment forbids a destructive reset."""
    env = str(getattr(settings, "app_env", "") or "").strip().lower()
    return env in PRODUCTION_ENVS


def _check_reset_allowed(reset: bool, force: bool) -> None:
    """Refuse ``--reset`` in production unless ``--force`` was passed."""
    if reset and _is_production() and not force:
        raise SystemExit(
            "refusing to --reset: APP_ENV="
            f"{settings.app_env!r} is production. Re-run with --force to confirm "
            "that every table may be dropped."
        )


def _table_of(model: Any) -> Any:
    """Return the ``Table`` for a model class or an already-unwrapped table."""
    return model if hasattr(model, "columns") else model.__table__


def _apply_sqlite_compat(engine_: AsyncEngine) -> bool:
    """Adapt the metadata so ``create_all`` works on SQLite. Returns ``True`` if applied.

    Two PostgreSQL-isms need translating:

    1. ``CHECK (char_length(text) <= 4096)`` - SQLite has ``length()``, not
       ``char_length()``, so ``CREATE TABLE`` would fail outright.
    2. ``BIGINT PRIMARY KEY`` with ``Identity()`` - SQLite only auto-populates a
       ``rowid``-aliased column when the declared type is exactly ``INTEGER``.
       Without this, ``group_messages.id`` stays ``NULL`` and the
       self-referencing ``reply_to_id`` / ``sla_incidents`` links break.

    PostgreSQL metadata is never touched: this function returns early unless the
    engine dialect is SQLite.
    """
    if engine_.dialect.name != "sqlite":
        return False
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
        for constraint in list(table.constraints):
            sqltext = getattr(constraint, "sqltext", None)
            if sqltext is not None and "char_length(" in str(sqltext):
                table.constraints.discard(constraint)
    LOG.info("sqlite target detected: applying test-only schema adaptations")
    return True


async def _prepare_schema(reset: bool, stats: SeedStats) -> None:
    """Optionally drop everything, then create the schema."""
    stats.sqlite_compat = _apply_sqlite_compat(engine)
    if reset:
        async with engine.begin() as conn:
            try:
                names = await conn.run_sync(
                    lambda sync_conn: sorted(inspect(sync_conn).get_table_names())
                )
            except SQLAlchemyError:  # pragma: no cover - empty database
                names = []
            await conn.run_sync(Base.metadata.drop_all)
            stats.tables_dropped = [name for name in names if name in Base.metadata.tables]
        LOG.warning("dropped %d table(s): %s", len(stats.tables_dropped),
                    ", ".join(stats.tables_dropped) or "<none>")
    await init_models()


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #


def _role_value(role: str) -> Any:
    """Pass the enum member through when the models expose ``OperatorRole``."""
    if OperatorRole is None:  # pragma: no cover - depends on the model module
        return role
    try:
        return OperatorRole(role)
    except ValueError:  # pragma: no cover
        return role


async def _upsert_operators(
    session: AsyncSession, specs: list[OperatorSpec], now: datetime, stats: SeedStats
) -> dict[str, Operator]:
    """Insert missing operators; existing usernames are left untouched.

    Password hashes are computed **lazily, once per ``password_kind``**: bcrypt is
    deliberately slow, so the admin, the supervisor and the ``op*`` accounts cost
    exactly three hashes no matter how many operators are seeded.

    When ``settings.backend_supervisor_username`` was already seeded as
    ``SUPERVISOR`` by an older run, this pass leaves it alone and seeds the
    administrator under :data:`ADMIN_USERNAME`; if that username is *taken* by a
    pre-existing supervisor (the default ``admin``), the supervisor is renamed to
    :data:`SUPERVISOR_USERNAME` so both roles keep a working login.
    """
    hashes: dict[str, str] = {}

    def _password_hash(kind: str) -> str:
        if kind not in hashes:
            if kind == "admin":
                hashes[kind] = hash_password(ADMIN_PASSWORD)
            elif kind == "supervisor":
                hashes[kind] = hash_password(settings.backend_supervisor_password)
            else:
                hashes[kind] = hash_password(settings.backend_seed_operator_password)
        return hashes[kind]

    async def _load(username: str) -> Operator | None:
        return (
            await session.execute(select(Operator).where(Operator.username == username))
        ).scalar_one_or_none()

    operators: dict[str, Operator] = {}
    taken = {spec.username for spec in specs}
    for spec in specs:
        existing = await _load(spec.username)
        if existing is not None and spec.username == ADMIN_USERNAME and spec.role == ROLE_ADMIN:
            # Default configuration collides with a supervisor seeded by an older
            # run (``backend_supervisor_username`` defaults to "admin"): move the
            # supervisor aside instead of overwriting a live account.
            if SUPERVISOR_USERNAME in taken:
                LOG.warning(
                    "operator %r already exists and %r is taken; administrator seeded "
                    "under an unused username would be impossible — leaving the "
                    "existing account untouched",
                    spec.username,
                    SUPERVISOR_USERNAME,
                )
            else:
                existing.username = SUPERVISOR_USERNAME
                existing.role = _role_value(ROLE_SUPERVISOR)
                existing.is_supervisor = True
                existing.password_hash = _password_hash("supervisor")
                existing.updated_at = now
                await session.flush()
                LOG.info(
                    "renamed pre-existing supervisor %r -> %r to free the %r administrator "
                    "account",
                    ADMIN_USERNAME,
                    SUPERVISOR_USERNAME,
                    ADMIN_USERNAME,
                )
                existing = None
        if existing is not None:
            operators[spec.username] = existing
            stats.operators_skipped += 1
            continue
        operator = Operator(
            username=spec.username,
            # ``is_supervisor`` is a derived property, never a column: the role
            # below is what decides which password hash the account receives.
            password_hash=_password_hash(spec.password_kind),
            display_name=spec.display_name,
            role=_role_value(spec.role),
            shift_status=spec.shift_status,
            is_online_ws=spec.is_online_ws,
            last_seen_at=now - timedelta(minutes=spec.last_seen_offset),
            sla_target_seconds=spec.sla_target_seconds,
            created_at=now - timedelta(days=90),
            updated_at=now - timedelta(minutes=spec.last_seen_offset),
            is_active=spec.is_active,
            max_chats=spec.max_chats,
            can_delete_messages=spec.can_delete_messages,
            can_export=spec.can_export,
            avatar_color=spec.avatar_color,
        )
        session.add(operator)
        await session.flush()
        operators[spec.username] = operator
        stats.operators_created += 1
    LOG.info("operators: %d created, %d already present",
             stats.operators_created, stats.operators_skipped)
    return operators


async def _upsert_chats(
    session: AsyncSession,
    operators: dict[str, Operator],
    now: datetime,
    stats: SeedStats,
) -> dict[str, GroupChat]:
    """Insert missing chats; existing ones keep their live assignment state."""
    chats: dict[str, GroupChat] = {}
    for spec in CHAT_SPECS:
        existing = (
            await session.execute(
                select(GroupChat).where(GroupChat.tg_chat_id == spec.tg_chat_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            chats[spec.key] = existing
            stats.chats_skipped += 1
            continue
        assignee = (
            operators.get(spec.assigned_username)
            if spec.assigned_username is not None
            else None
        )
        if spec.assigned_username is not None and assignee is None:
            LOG.warning("chat %s: assigned operator %r does not exist, leaving unassigned",
                        spec.key, spec.assigned_username)
        chat = GroupChat(
            tg_chat_id=spec.tg_chat_id,
            title=spec.title,
            username=spec.username,
            merchant_code=spec.merchant_code,
            unread_count=spec.unread_count,
            assigned_operator_id=None if assignee is None else assignee.id,
            last_message_at=now - timedelta(minutes=spec.last_offset_minutes),
            created_at=now - timedelta(hours=spec.created_offset_hours),
            updated_at=now - timedelta(minutes=spec.last_offset_minutes),
        )
        session.add(chat)
        await session.flush()
        chats[spec.key] = chat
        stats.chats_created += 1
    LOG.info("chats: %d created, %d already present", stats.chats_created, stats.chats_skipped)
    return chats


async def _insert_messages(
    session: AsyncSession,
    chat: GroupChat,
    operators: dict[str, Operator],
    now: datetime,
    stats: SeedStats,
) -> dict[int, GroupMessage]:
    """Insert every message of ``chat`` and return ``seq -> ORM object``."""
    specs = build_messages(_chat_key_of(chat))
    by_seq: dict[int, GroupMessage] = {}
    for spec in specs:
        operator = (
            operators.get(spec.sender_operator_username)
            if spec.sender_operator_username is not None
            else None
        )
        if spec.sender_operator_username is not None and operator is None:
            LOG.warning("chat %s: operator %r missing, dropping attribution",
                        spec.chat_key, spec.sender_operator_username)
        created_at = now - timedelta(minutes=spec.offset_minutes)
        quoted = by_seq.get(spec.reply_to_seq) if spec.reply_to_seq is not None else None
        message = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=spec.tg_msg_id,
            sender_type=spec.sender_type,
            sender_operator_id=None if operator is None else operator.id,
            sender_name=spec.sender_name,
            sender_tg_id=spec.sender_tg_id,
            text=spec.text,
            reply_to_id=None if quoted is None else quoted.id,
            reply_to_tg_msg_id=None if quoted is None else quoted.tg_msg_id,
            phone=spec.phone,
            is_deleted_in_tg=spec.is_deleted_in_tg,
            # CONTRACTS_V5 §1.2: the CHECK requires the flag and its source to agree,
            # so every seeded tombstone declares where it came from.
            deleted_source=DELETED_SOURCE_TELEGRAM if spec.is_deleted_in_tg else None,
            deleted_at=(
                None
                if spec.deleted_after_minutes is None
                else created_at + timedelta(minutes=spec.deleted_after_minutes)
            ),
            sla_incident_id=None,
            delivery_status=spec.delivery_status,
            client_nonce=None,
            tx_status=spec.tx_status,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(message)
        await session.flush()
        by_seq[spec.seq] = message
        stats.messages_created += 1
    return by_seq


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive datetime as UTC (SQLite hands back naive values)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# --------------------------------------------------------------------------- #
# Demo media and reactions
# --------------------------------------------------------------------------- #


def _attachment_row(message: GroupMessage, spec: AttachmentSpec) -> MediaAttachment:
    """Build the ``media_attachments`` row of ``spec`` for ``message``."""
    return MediaAttachment(
        message_id=message.id,
        kind=spec.kind,
        file_name=spec.file_name,
        mime_type=spec.mime_type,
        size_bytes=spec.size_bytes,
        width=spec.width,
        height=spec.height,
        duration_seconds=spec.duration_seconds,
        storage_path=spec.storage_path,
        tg_file_id=spec.tg_file_id,
        thumbnail_path=spec.thumbnail_path,
        created_at=_as_utc(message.created_at),
    )


async def _insert_media_messages(
    session: AsyncSession,
    chat: GroupChat,
    messages: dict[int, GroupMessage],
    now: datetime,
    stats: SeedStats,
) -> dict[int, GroupMessage]:
    """Append the demo media messages of ``chat`` and their attachments.

    Idempotency uses the natural key ``(chat_id, tg_msg_id)`` exactly like the
    authored transcript: the demo ``tg_msg_id`` is
    ``TG_MSG_ID_START + last_authored_seq + offset``, so a second run finds the
    row and skips both the message and its attachments.
    """
    chat_key = _chat_key_of(chat)
    demos = [media for media in DEMO_MEDIA if media.chat_key == chat_key]
    if not demos:
        return {}
    last_authored = max(messages) if messages else -1
    by_offset: dict[int, GroupMessage] = {}
    for media in demos:
        tg_msg_id = TG_MSG_ID_START + last_authored + media.offset
        existing = (
            await session.execute(
                select(GroupMessage.id).where(
                    GroupMessage.chat_id == chat.id,
                    GroupMessage.tg_msg_id == tg_msg_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            stats.messages_skipped += 1
            stats.attachments_skipped += len(media.attachments)
            continue
        operator = (
            (
                await session.execute(
                    select(Operator).where(
                        Operator.username == media.sender_operator_username
                    )
                )
            ).scalar_one_or_none()
            if media.sender_operator_username is not None
            else None
        )
        if media.sender_operator_username is not None and operator is None:
            LOG.warning("chat %s: operator %r missing, dropping media attribution",
                        chat_key, media.sender_operator_username)
        created_at = now - timedelta(minutes=media.offset_minutes)
        quoted = (
            by_offset.get(media.reply_to_offset)
            if media.reply_to_offset is not None
            else None
        )
        message = GroupMessage(
            chat_id=chat.id,
            tg_msg_id=tg_msg_id,
            sender_type=media.sender_type,
            sender_operator_id=None if operator is None else operator.id,
            sender_name=media.sender_name,
            sender_tg_id=media.sender_tg_id,
            # Telegram keeps the caption out of ``text`` for a media message; the
            # demo rows follow the same rule so the UI renders one bubble.
            text="",
            reply_to_id=None if quoted is None else quoted.id,
            reply_to_tg_msg_id=None if quoted is None else quoted.tg_msg_id,
            phone=None,
            is_deleted_in_tg=media.is_deleted_in_tg,
            deleted_source=DELETED_SOURCE_TELEGRAM if media.is_deleted_in_tg else None,
            deleted_at=(
                None
                if media.deleted_after_minutes is None
                else created_at + timedelta(minutes=media.deleted_after_minutes)
            ),
            sla_incident_id=None,
            delivery_status=DeliveryStatus.SENT.value,
            client_nonce=None,
            tx_status=None,
            kind=media.kind,
            caption=media.caption,
            forwarded_from=None,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(message)
        await session.flush()
        for attachment_spec in media.attachments:
            session.add(_attachment_row(message, attachment_spec))
            stats.attachments_created += 1
        await session.flush()
        by_offset[media.offset] = message
        stats.messages_created += 1
        chat.last_message_at = _later_of(chat.last_message_at, created_at)
    LOG.info("chat %s: %d demo media message(s), %d attachment(s)",
             chat_key, len(by_offset), stats.attachments_created)
    return by_offset


async def _insert_reactions(
    session: AsyncSession,
    chat: GroupChat,
    messages: dict[int, GroupMessage],
    media_messages: dict[int, GroupMessage],
    operators: dict[str, Operator],
    now: datetime,
    stats: SeedStats,
) -> None:
    """Insert the planned reactions on authored and demo messages.

    A negative plan offset selects an authored message counted from the end of
    the transcript; a positive one selects a demo media message by its offset.
    Every ``(emoji, actor)`` pair is checked against the database first, so the
    seed stays idempotent even when an operator already reacted in the UI.
    """
    chat_key = _chat_key_of(chat)
    if not messages:
        return
    ordered = [messages[seq] for seq in sorted(messages)]
    for offset, reaction in build_reactions(chat_key):
        target = (
            ordered[offset] if offset < 0 else media_messages.get(offset)
        )
        if target is None or -offset > len(ordered):
            LOG.warning("chat %s: reaction target offset %s missing, skipped",
                        chat_key, offset)
            continue
        operator = (
            operators.get(reaction.operator_username)
            if reaction.operator_username is not None
            else None
        )
        if reaction.operator_username is not None and operator is None:
            LOG.warning("chat %s: reaction operator %r missing, skipped",
                        chat_key, reaction.operator_username)
            continue
        conditions = [
            MessageReaction.message_id == target.id,
            MessageReaction.emoji == reaction.emoji,
        ]
        if reaction.is_crm:
            conditions.append(MessageReaction.operator_id == operator.id)
        else:
            conditions.append(MessageReaction.tg_user_id == reaction.tg_user_id)
        existing = (
            await session.execute(select(MessageReaction.id).where(*conditions))
        ).scalar_one_or_none()
        if existing is not None:
            stats.reactions_skipped += 1
            continue
        # Back-dated slightly after its message so ordering is chronological.
        created_at = _as_utc(target.created_at) + timedelta(
            seconds=30 + stats.reactions_created
        )
        session.add(
            MessageReaction(
                message_id=target.id,
                operator_id=None if operator is None else operator.id,
                emoji=reaction.emoji,
                tg_user_id=reaction.tg_user_id,
                source=reaction.source,
                created_at=min(created_at, now.replace(microsecond=0)),
            )
        )
        stats.reactions_created += 1
    LOG.info("chat %s: reactions created %d, skipped %d",
             chat_key, stats.reactions_created, stats.reactions_skipped)

def _later_of(current: datetime | None, candidate: datetime) -> datetime:
    """Return the later of two (possibly naive/aware mixed) datetimes."""
    if current is None:
        return candidate
    later = candidate if _as_utc(candidate) >= _as_utc(current) else current
    return _as_utc(later)


def _chat_key_of(chat: GroupChat) -> str:
    """Map a persisted chat back to its ``ChatSpec`` key."""
    for spec in CHAT_SPECS:
        if spec.tg_chat_id == chat.tg_chat_id:
            return spec.key
    raise KeyError(f"chat {chat.tg_chat_id!r} is not part of the seed catalogue")


async def _chat_has_messages(session: AsyncSession, chat_id: int) -> bool:
    """``True`` when the chat already has history (seeded or live)."""
    found = (
        await session.execute(
            select(GroupMessage.id).where(GroupMessage.chat_id == chat_id).limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


_LIVE_OFFSETS: Final[dict[str, tuple[int, int]]] = {
    # plan: (seconds since the opener, seconds until the deadline)
    # ``SLA_TARGET_SECONDS - elapsed`` equals the deadline offset, so every live
    # incident carries a consistent 300 s budget measured from its opener.
    "due_soon": (60, 240),    # 60 s ago  -> 240 s left
    "burning": (260, 40),     # 260 s ago -> 40 s left
    "overdue": (420, -120),   # 420 s ago -> 120 s past due
}


async def _insert_incidents(
    session: AsyncSession,
    chat: GroupChat,
    messages: dict[int, GroupMessage],
    operators: dict[str, Operator],
    now: datetime,
    stats: SeedStats,
) -> None:
    """Create the incidents of ``chat`` and wire the message back-references.

    The three live incidents are re-anchored to the wall clock: a 48-hour-old
    history cannot carry a "burning" countdown, so the opener is moved to
    ``now - N minutes`` and the deadline is set from the offset table.
    """
    chat_key = _chat_key_of(chat)
    for spec in build_incidents():
        if spec.chat_key != chat_key:
            continue
        trigger = messages.get(spec.trigger_seq)
        if trigger is None:
            LOG.warning("incident %s: trigger seq %d missing, skipped",
                        spec.key, spec.trigger_seq)
            continue
        existing = (
            await session.execute(
                select(SLAIncident.id).where(
                    SLAIncident.chat_id == chat.id,
                    SLAIncident.message_id == trigger.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            stats.incidents_skipped += 1
            continue
        assignee = (
            operators.get(spec.assigned_username)
            if spec.assigned_username is not None
            else None
        )
        if spec.plan == "resolved":
            if spec.response_seconds is None:
                raise ValueError(f"incident {spec.key}: resolved without response time")
            triggered_at = _as_utc(trigger.created_at)
            resolved_at = triggered_at + timedelta(seconds=spec.response_seconds)
            resolver = messages.get(spec.resolve_seq) if spec.resolve_seq else None
            incident = SLAIncident(
                chat_id=chat.id,
                message_id=trigger.id,
                assigned_operator_id=None if assignee is None else assignee.id,
                status=SlaIncidentStatus.RESOLVED.value,
                deadline_at=triggered_at + timedelta(seconds=SLA_TARGET_SECONDS),
                resolved_at=resolved_at,
                resolved_by_message_id=None if resolver is None else resolver.id,
                response_time_seconds=spec.response_seconds,
                escalated=spec.response_seconds > SLA_TARGET_SECONDS,
                created_at=triggered_at,
            )
        else:
            elapsed_seconds, deadline_offset = _LIVE_OFFSETS[spec.plan]
            # A live incident has to read consistently from every angle:
            #   * the SLA clock starts on the triggering message,
            #   * ``created_at + SLA_TARGET_SECONDS`` is the real budget,
            #   * the countdown visible at seed time equals ``deadline_offset``.
            # All three hold only if the incident's ``created_at`` *is* the
            # trigger's time, because the engine measures responses from
            # ``sla_incidents.created_at`` while the UI shows the deadline. The
            # opener therefore has to be recent, which is why the live chats carry
            # their opener as the newest message (see ``build_messages``): a
            # 48-hour-old transcript cannot host a burning countdown.
            triggered_at = _as_utc(trigger.created_at)
            incident = SLAIncident(
                chat_id=chat.id,
                message_id=trigger.id,
                assigned_operator_id=None if assignee is None else assignee.id,
                status=SlaIncidentStatus.PENDING.value,
                deadline_at=triggered_at + timedelta(seconds=SLA_TARGET_SECONDS),
                resolved_at=None,
                resolved_by_message_id=None,
                response_time_seconds=None,
                escalated=spec.plan == "overdue",
                created_at=triggered_at,
            )
        session.add(incident)
        await session.flush()
        # The message back-references are written with explicit UPDATEs instead of
        # attribute assignment. ``group_messages`` is inserted newest-first, so an
        # ORM flush would apply ``TimestampMixin.onupdate`` and stamp
        # ``updated_at = now`` onto rows with older ``created_at`` values - which
        # corrupts ordering and reply relationships for API paths that use it.
        await _link_message(session, trigger.id, incident.id, triggered_at)
        if incident.status == SlaIncidentStatus.RESOLVED.value:
            resolver = messages.get(spec.resolve_seq) if spec.resolve_seq else None
            if resolver is not None:
                await _link_message(
                    session, resolver.id, incident.id, _as_utc(resolver.created_at)
                )
        elif spec.plan in _LIVE_OFFSETS:
            # ``created_at`` keeps its historical value so the transcript stays
            # contiguous: the live countdown lives on ``sla_incidents`` only.
            chat.last_message_at = _later_of(chat.last_message_at, trigger.created_at)
        await session.flush()
        stats.incidents_created += 1
    LOG.info("chat %s: incidents created so far %d", chat_key, stats.incidents_created)


async def _link_message(
    session: AsyncSession, message_id: int, incident_id: int, created_at: datetime
) -> None:
    """Set ``group_messages.sla_incident_id`` while pinning ``updated_at``.

    ``TimestampMixin.updated_at`` carries ``onupdate=utcnow()``, which SQLAlchemy
    applies to *any* UPDATE - including this Core statement. Supplying the value
    explicitly keeps the seeded row internally consistent instead of stamping a
    back-dated message with "now".
    """
    await session.execute(
        update(GroupMessage)
        .where(GroupMessage.id == message_id)
        .values(sla_incident_id=incident_id, updated_at=_as_utc(created_at))
    )


async def _insert_audits(
    session: AsyncSession,
    operators: dict[str, Operator],
    now: datetime,
    stats: SeedStats,
) -> None:
    """Insert missing ``tx_lookup_audit`` rows."""
    for spec in AUDIT_SPECS:
        operator = operators.get(spec.operator_username)
        if operator is None:
            LOG.warning("audit row for %r skipped: operator missing", spec.operator_username)
            continue
        # ``tx_lookup_audit`` is an append-only log: the same operator may legitimately
        # look up the same TX on the same panel many times, so several rows can match
        # this predicate. Only existence matters for idempotency.
        existing = (
            await session.execute(
                select(TxLookupAudit.id)
                .where(
                    TxLookupAudit.operator_id == operator.id,
                    TxLookupAudit.tx_id == spec.tx_id,
                    TxLookupAudit.admin_panel == spec.admin_panel,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            stats.audits_skipped += 1
            continue
        session.add(
            TxLookupAudit(
                operator_id=operator.id,
                tx_id=spec.tx_id,
                admin_panel=spec.admin_panel,
                found=spec.found,
                response_status=spec.response_status,
                amount=spec.amount,
                lookup_ms=spec.lookup_ms,
                created_at=now - timedelta(seconds=spec.offset_seconds),
            )
        )
        stats.audits_created += 1
    LOG.info("tx_lookup_audit: %d created, %d already present",
             stats.audits_created, stats.audits_skipped)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def seed(
    reset: bool = False,
    history: bool = True,
    operators: int = DEFAULT_OPERATOR_COUNT,
    force: bool = False,
) -> dict[str, int]:
    """Seed the database and return a mapping of counters.

    Parameters
    ----------
    reset:
        Drop every table declared in ``app.models`` before creating and seeding.
        Refused when ``settings.app_env`` is production unless ``force`` is set.
    history:
        When ``False`` only operators and chats are upserted.
    operators:
        How many ``op1..opN`` accounts to create (1..999).
    force:
        Allow ``reset`` in production.

    The whole write is one transaction: a failure rolls everything back, so a
    partially seeded database can never be left behind.
    """
    _check_reset_allowed(reset, force)
    operator_specs = build_operators(operators)
    stats = SeedStats(reset=reset, history=history)
    now = datetime.now(UTC).replace(microsecond=0)
    LOG.info("seeding %s (env=%s, reset=%s, history=%s, operators=%d)",
             settings.database_url_safe, settings.app_env, reset, history, operators)
    await _prepare_schema(reset, stats)
    async with SessionLocal() as session:
        try:
            ops = await _upsert_operators(session, operator_specs, now, stats)
            chats = await _upsert_chats(session, ops, now, stats)
            if history:
                for spec in CHAT_SPECS:
                    chat = chats[spec.key]
                    if await _chat_has_messages(session, chat.id):
                        stats.history_skipped_chats += 1
                        LOG.info("chat %s already has messages, history left untouched",
                                 spec.merchant_code)
                        continue
                    messages = await _insert_messages(session, chat, ops, now, stats)
                    media_messages = await _insert_media_messages(
                        session, chat, messages, now, stats
                    )
                    await _insert_incidents(session, chat, messages, ops, now, stats)
                    await _insert_reactions(
                        session, chat, messages, media_messages, ops, now, stats
                    )
                LOG.info("messages: %d created, history kept for %d pre-populated chat(s)",
                         stats.messages_created, stats.history_skipped_chats)
                LOG.info("media/reactions: %d attachment(s), %d reaction(s) created",
                         stats.attachments_created, stats.reactions_created)
            await _insert_audits(session, ops, now, stats)
            await _insert_quick_replies(session, now, stats)
            await session.commit()
        except (SQLAlchemyError, ValueError, KeyError):
            await session.rollback()
            LOG.error("seed failed, transaction rolled back\n%s", traceback.format_exc())
            raise
    LOG.info("seed finished: %s", stats.as_counts())
    return stats.as_counts()


def seed_sync(**kwargs: Any) -> dict[str, int]:
    """Blocking wrapper around :func:`seed` (convenience for tests/scripts)."""
    return asyncio.run(seed(**kwargs))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (kept separate so tests can reuse it)."""
    parser = argparse.ArgumentParser(
        prog="seed",
        description="Seed the TG-Desk CRM database (idempotent by default).",
    )
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate every table before seeding")
    parser.add_argument("--force", action="store_true",
                        help="allow --reset while APP_ENV=production")
    parser.add_argument("--operators", type=int, default=DEFAULT_OPERATOR_COUNT,
                        metavar="N",
                        help=f"number of operators op1..opN (default {DEFAULT_OPERATOR_COUNT})")
    parser.add_argument("--with-history", dest="history", action="store_true", default=True,
                        help="seed messages, SLA incidents and tx audits (default)")
    parser.add_argument("--no-history", dest="history", action="store_false",
                        help="only seed operators and chats")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable DEBUG logging")
    return parser


def _configure_logging(verbose: bool) -> None:
    """Route seeder output through ``logging`` (never ``print``) for data flow."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )
        root.addHandler(handler)
    root.setLevel(level)
    LOG.setLevel(level)


def _summary_lines(counts: dict[str, int], args: argparse.Namespace) -> list[str]:
    """Build the final human-facing summary (the only ``print`` in this file)."""
    width = max(len(key) for key in counts)
    mode = "reset" if args.reset else "upsert"
    if args.force and args.reset:
        mode += " (forced)"
    lines = [
        "",
        "=" * 66,
        "  TG-Desk CRM - seed complete",
        "=" * 66,
        f"  mode       : {mode} / history={'on' if args.history else 'off'}"
        f" / operators={args.operators}",
        f"  environment: {settings.app_env}",
        f"  database   : {settings.database_url_safe}",
        "-" * 66,
    ]
    for key, value in counts.items():
        lines.append(f"  {key.ljust(width)} : {value}")
    lines.extend([
        "-" * 66,
        "  credentials",
        f"    admin      : {ADMIN_USERNAME} / {ADMIN_PASSWORD}   "
        f"(role ADMIN, /admin login)",
        f"    supervisor : {supervisor_username()} / "
        f"{settings.backend_supervisor_password}   (role SUPERVISOR)",
        f"    operators  : op1 .. op{args.operators} / "
        f"{settings.backend_seed_operator_password}",
        "    examples   : " + ", ".join(
            f"op{index} / {settings.backend_seed_operator_password}"
            for index in (1, 3, 5, 7) if index <= args.operators
        ),
        "=" * 66,
        "",
    ])
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    if sys.platform == "win32":
        # asyncpg works best with the selector policy on Windows: the Proactor
        # loop cannot handle the socket/DNS semantics the pool expects.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # passlib 1.7.4 probes bcrypt's removed ``__about__`` attribute; keep the
    # harmless warning out of the operator's terminal.
    warnings.filterwarnings("ignore", message=r".*error reading bcrypt version.*")
    warnings.filterwarnings("ignore", message=r".*trapped.*error reading bcrypt version.*")

    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    if not 1 <= args.operators <= MAX_OPERATOR_COUNT:
        print(f"--operators must be between 1 and {MAX_OPERATOR_COUNT}", file=sys.stderr)
        return 2
    try:
        _check_reset_allowed(args.reset, args.force)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 3

    try:
        counts = asyncio.run(
            seed(
                reset=args.reset,
                history=args.history,
                operators=args.operators,
                force=args.force,
            )
        )
    except SystemExit as exc:  # re-raised by the guard inside seed()
        print(str(exc), file=sys.stderr)
        return 3
    except (SQLAlchemyError, OSError) as exc:
        LOG.error("seeding aborted: %s: %s", type(exc).__name__, exc)
        return 1

    print("\n".join(_summary_lines(counts, args)))
    return 0




# --------------------------------------------------------------------------- #
# Quick replies (CONTRACTS_V5 5.4)
# --------------------------------------------------------------------------- #

#: The four canned answers that used to be hard-coded in
#: ``frontend/src/components/MessageInput.tsx`` (``QUICK_REPLIES``), moved into the
#: ``quick_replies`` table by 5.4. The ``body`` strings are byte-identical to the
#: removed constant - operators have these answers in muscle memory, and any edit
#: to them is a product decision, not a seeding detail.
QUICK_REPLY_SPECS: Final[tuple[QuickReplySpec, ...]] = (
    QuickReplySpec(
        shortcut="thanks",
        title="Благодарность",
        body=(
            "Thanks for reaching out! We are checking this now and will update you "
            "within a few minutes. \U0001F64C"
        ),
        sort_order=10,
    ),
    QuickReplySpec(
        shortcut="txid",
        title="Запросить TX ID",
        body=(
            "Could you please share the transaction ID so we can pull the gateway log?"
        ),
        sort_order=20,
    ),
    QuickReplySpec(
        shortcut="escalated",
        title="Эскалация",
        body=(
            "This has been escalated to the settlement team \u2014 ETA for a full "
            "answer is 30 minutes."
        ),
        sort_order=30,
    ),
    QuickReplySpec(
        shortcut="resolved",
        title="Решено",
        body=(
            "Confirmed and resolved on our side. Please let us know if anything still "
            "looks wrong."
        ),
        sort_order=40,
    ),
)

#: ``quick_replies.category`` the seeded replies carry (5.1 default).
QUICK_REPLY_SEED_CATEGORY: Final[str] = "general"


async def _insert_quick_replies(
    session: AsyncSession,
    now: datetime,
    stats: SeedStats,
) -> None:
    """Insert the 5.4 quick replies that are missing, keyed by ``shortcut``.

    Idempotent by natural key: a re-run leaves existing rows untouched, so an
    administrator who renamed, retitled or reordered a seeded reply never has that
    edit overwritten by the seeder. New rows are attributed to the seeded
    administrator, which is who would have created them by hand.
    """
    author_id = (
        await session.execute(
            select(Operator.id).where(Operator.username == ADMIN_USERNAME).limit(1)
        )
    ).scalar_one_or_none()
    existing: set[str] = set(
        (
            await session.execute(
                select(QuickReply.shortcut).where(
                    QuickReply.shortcut.in_([spec.shortcut for spec in QUICK_REPLY_SPECS])
                )
            )
        )
        .scalars()
        .all()
    )
    for spec in QUICK_REPLY_SPECS:
        if spec.shortcut in existing:
            stats.quick_replies_skipped += 1
            continue
        session.add(
            QuickReply(
                shortcut=spec.shortcut,
                title=spec.title,
                body=spec.body,
                category=QUICK_REPLY_SEED_CATEGORY,
                sort_order=spec.sort_order,
                is_active=True,
                created_by_id=author_id,
                created_at=now,
                updated_at=now,
            )
        )
        stats.quick_replies_created += 1
    LOG.info(
        "quick_replies: %d created, %d already present",
        stats.quick_replies_created,
        stats.quick_replies_skipped,
    )

if __name__ == "__main__":
    raise SystemExit(main())

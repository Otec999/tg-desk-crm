"""SQLAlchemy 2.0 ORM models for the TG-Desk CRM backend.

Design notes
------------
* This module is a **leaf**: it imports nothing from the rest of ``app`` except
  :mod:`app.enums`, which itself imports nothing. ``app/database.py`` therefore
  performs a lazy ``from app.models import Base`` to obtain ``metadata`` without
  creating an import cycle with ``app.config``.
* Every enum-ish column is stored as ``String(24)`` guarded by a
  ``CheckConstraint`` (portable, no PostgreSQL ``CREATE TYPE`` migration pain)
  and writes go through ``@validates`` so callers may pass either the enum
  member or its raw string.
* ``server_default`` is used for every column a writer may omit, so rows created
  by plain ``INSERT ... VALUES`` (seed, worker, ad-hoc SQL) behave like ORM
  inserts.
* Datetimes are ``TIMESTAMP WITH TIME ZONE`` end to end; JSON serialisation adds
  the ``Z`` suffix in :mod:`app.schemas`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

from app.enums import (
    DeliveryStatus,
    EditSource,
    MessageKind,
    MessageSenderType,
    OperatorRole,
    ReactionEmoji,
    ShiftStatus,
    SlaIncidentStatus,
    TxAdminPanel,
    TxStatus,
    _StrEnum,
)

__all__ = [
    "APP_SETTINGS_ID",
    "AUDIT_ACTION_CREATE_ACCOUNT",
    "AUDIT_ACTION_CREATE_OPERATOR",
    "AUDIT_ACTION_DEACTIVATE_OPERATOR",
    "AUDIT_ACTION_DELETE_ACCOUNT",
    "AUDIT_ACTION_RESET_PASSWORD",
    "AUDIT_ACTION_UPDATE_ACCOUNT",
    "AUDIT_ACTION_UPDATE_OPERATOR",
    "AUDIT_ACTION_UPDATE_SETTINGS",
    "AUDIT_ACTION_VALUES",
    "AppSettings",
    "AuditEntry",
    "Base",
    "DEFAULT_QUICK_REPLY_CATEGORY",
    "DEFAULT_SLA_TARGET_SECONDS",
    "DELETED_SOURCE_CRM",
    "DELETED_SOURCE_TELEGRAM",
    "DeletedSource",
    "DeliveryStatus",
    "ENUM_LENGTH",
    "EditSource",
    "GroupChat",
    "GroupMessage",
    "MediaAttachment",
    "MessageEdit",
    "MessageKind",
    "MessageReaction",
    "MessageSenderType",
    "Operator",
    "OperatorRole",
    "QuickReply",
    "REACTION_SOURCE_CRM",
    "REACTION_SOURCE_TELEGRAM",
    "ReactionEmoji",
    "ReactionSource",
    "SHORT_ENUM_LENGTH",
    "SLAIncident",
    "SLA_TARGET_SECONDS_MAX",
    "SLA_TARGET_SECONDS_MIN",
    "ShiftStatus",
    "SlaIncidentStatus",
    "TelegramAccount",
    "TimestampMixin",
    "TxAdminPanel",
    "TxLookupAudit",
    "TxStatus",
    "enum_value",
    "utcnow",
]

#: Width of every VARCHAR-backed enum column. All CONTRACTS.md §2 values are
#: <= 10 characters; 24 leaves room for additive values without a migration.
ENUM_LENGTH = 24

#: Width of the media/reaction enum columns: ``MessageKind``/``ReactionEmoji``
#: members can be multiple characters (``DOCUMENT``, ``❤️``), so 16 is the
#: contract's own budget (CONTRACTS_V2.md §2.1/§2.2) and still covers the longest
#: member in UTF-8 bytes.
SHORT_ENUM_LENGTH = 16

#: Characters accepted by :meth:`Operator._validate_avatar_color`.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: ``message_reactions.source`` values (CONTRACTS_V2.md §2.2): a reaction is
#: either placed by a CRM operator or mirrored from Telegram itself.
REACTION_SOURCE_CRM = "CRM"
REACTION_SOURCE_TELEGRAM = "TELEGRAM"


class ReactionSource(_StrEnum):
    """Closed set of ``message_reactions.source`` values (forbids ``"OTHER"``)."""

    CRM = REACTION_SOURCE_CRM
    TELEGRAM = REACTION_SOURCE_TELEGRAM


#: ``group_messages.deleted_source`` values (CONTRACTS_V5 §1.2). Same two words as
#: :data:`REACTION_SOURCE_CRM` / :data:`REACTION_SOURCE_TELEGRAM` on purpose: the
#: contract spells the source vocabulary once, and ``NULL`` (not a member) means
#: "the row is not deleted". Kept as a dedicated pair rather than reusing the
#: reaction constants so a future divergence changes one place, not two.
DELETED_SOURCE_CRM = "CRM"
DELETED_SOURCE_TELEGRAM = "TELEGRAM"


class DeletedSource(_StrEnum):
    """Closed set of ``group_messages.deleted_source`` values (feeds the ``CHECK``)."""

    CRM = DELETED_SOURCE_CRM
    TELEGRAM = DELETED_SOURCE_TELEGRAM


# ---------------------------------------------------------------------------
# app_settings defaults (CONTRACTS_V5 §2.1)
# ---------------------------------------------------------------------------

#: Primary key of the single settings row. One row, always ``id = 1``.
APP_SETTINGS_ID = 1
#: Global default SLA target in seconds when no per-operator override exists.
DEFAULT_SLA_TARGET_SECONDS = 1800
#: Inclusive bounds of ``app_settings.sla_target_seconds`` (CONTRACTS_V5 §2.1):
#: below 60 s the clock is unusable, above 24 h it is no longer an SLA.
SLA_TARGET_SECONDS_MIN = 60
SLA_TARGET_SECONDS_MAX = 86400


# ---------------------------------------------------------------------------
# quick_replies defaults (CONTRACTS_V5 §5.1)
# ---------------------------------------------------------------------------

#: Default ``quick_replies.category`` for a reply that does not pick one.
DEFAULT_QUICK_REPLY_CATEGORY = "general"


#: ``admin_audit_log`` action vocabulary. Every admin mutation writes exactly one of
#: these, so the journal can be filtered/rendered without free-text guessing. They
#: are declared here (not in :mod:`app.enums`) because the table is the only
#: consumer; ``app.services.admin`` imports them and never re-spells the strings.
AUDIT_ACTION_CREATE_OPERATOR = "create_operator"
AUDIT_ACTION_UPDATE_OPERATOR = "update_operator"
AUDIT_ACTION_DEACTIVATE_OPERATOR = "deactivate_operator"
AUDIT_ACTION_RESET_PASSWORD = "reset_password"
AUDIT_ACTION_CREATE_ACCOUNT = "create_account"
AUDIT_ACTION_UPDATE_ACCOUNT = "update_account"
AUDIT_ACTION_DELETE_ACCOUNT = "delete_account"
#: Authentication events are audited too: an admin login is exactly the kind of action
#: that must be traceable after an incident.
AUDIT_ACTION_ADMIN_LOGIN = "admin_login"
AUDIT_ACTION_ADMIN_LOGIN_FAILED = "admin_login_failed"
AUDIT_ACTION_ADMIN_LOGOUT = "admin_logout"
#: CONTRACTS_V5 2.3: writing app_settings (SLA on/off, global target, the operator-reply
#: reset rule) is journaled like any other admin mutation.
AUDIT_ACTION_UPDATE_SETTINGS = "update_settings"

#: Every audit action the admin panel is allowed to write.
AUDIT_ACTION_VALUES: tuple[str, ...] = (
    AUDIT_ACTION_CREATE_OPERATOR,
    AUDIT_ACTION_UPDATE_OPERATOR,
    AUDIT_ACTION_DEACTIVATE_OPERATOR,
    AUDIT_ACTION_RESET_PASSWORD,
    AUDIT_ACTION_CREATE_ACCOUNT,
    AUDIT_ACTION_UPDATE_ACCOUNT,
    AUDIT_ACTION_DELETE_ACCOUNT,
    AUDIT_ACTION_ADMIN_LOGIN,
    AUDIT_ACTION_ADMIN_LOGIN_FAILED,
    AUDIT_ACTION_ADMIN_LOGOUT,
    AUDIT_ACTION_UPDATE_SETTINGS,
)


class _AuditAction(_StrEnum):
    """Enum view over :data:`AUDIT_ACTION_VALUES` (feeds the ``CHECK`` constraint)."""

    CREATE_OPERATOR = AUDIT_ACTION_CREATE_OPERATOR
    UPDATE_OPERATOR = AUDIT_ACTION_UPDATE_OPERATOR
    DEACTIVATE_OPERATOR = AUDIT_ACTION_DEACTIVATE_OPERATOR
    RESET_PASSWORD = AUDIT_ACTION_RESET_PASSWORD
    CREATE_ACCOUNT = AUDIT_ACTION_CREATE_ACCOUNT
    UPDATE_ACCOUNT = AUDIT_ACTION_UPDATE_ACCOUNT
    DELETE_ACCOUNT = AUDIT_ACTION_DELETE_ACCOUNT
    ADMIN_LOGIN = AUDIT_ACTION_ADMIN_LOGIN
    ADMIN_LOGIN_FAILED = AUDIT_ACTION_ADMIN_LOGIN_FAILED
    ADMIN_LOGOUT = AUDIT_ACTION_ADMIN_LOGOUT
    UPDATE_SETTINGS = AUDIT_ACTION_UPDATE_SETTINGS


#: Width of ``message_edits.source`` (CONTRACTS_V3 §2.2): ``TELEGRAM`` / ``CRM``.
EDIT_SOURCE_LENGTH = 16


class _EditSource(_StrEnum):
    """Enum view over :class:`~app.enums.EditSource` (feeds the ``CHECK`` constraint).

    Mirrors the ``_AuditAction`` pattern: the column is a small ``VARCHAR`` guarded
    by a ``CHECK`` built from :func:`_check_values`, so the database enforces the
    closed vocabulary even for plain ``INSERT``s written by the worker.
    """

    TELEGRAM = EditSource.TELEGRAM.value
    CRM = EditSource.CRM.value


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp (single source of truth)."""
    return datetime.now(timezone.utc)


def enum_value(value: Any, enum_cls: type) -> Any:
    """Coerce ``value`` to ``enum_cls`` and return its raw ``str`` value.

    Accepts an enum member, its string value (any case, because the API layer
    normalises user input to uppercase) or ``None``. Returns ``None`` unchanged so
    nullable columns keep working. Raises ``LookupError`` for unknown values so a
    bad write fails loudly instead of silently persisting garbage.
    """
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.value
    if isinstance(value, str):
        candidate = value.strip()
        try:
            return enum_cls(candidate).value
        except ValueError:
            pass
        upper = candidate.upper()
        try:
            return enum_cls(upper).value
        except ValueError:
            member = getattr(enum_cls, upper, None)
            if member is not None:
                return member.value
            valid = ", ".join(item.value for item in enum_cls)
            raise LookupError(f"invalid {enum_cls.__name__}: {value!r} (valid: {valid})") from None
    raise LookupError(f"invalid {enum_cls.__name__}: {value!r}")


def _check_values(column: str, enum_cls: type) -> str:
    """Build a portable ``CHECK (col IN ('A', 'B'))`` SQL fragment for an enum."""
    quoted = ", ".join(f"'{item.value}'" for item in enum_cls)
    return f"{column} IN ({quoted})"


# ---------------------------------------------------------------------------
# Declarative base with a deterministic constraint naming convention
# ---------------------------------------------------------------------------

# NOTE: SQLAlchemy only expands ``%(constraint_name)s`` for constraints that have
# an explicit ``name=``; it never derives one from the column list. Every
# CheckConstraint below therefore sets a short, unique, snake_case name.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every model (and by Alembic autogenerate)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - concrete models override this
        return f"<{type(self).__name__} id={getattr(self, 'id', None)!r}>"


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` (both timezone-aware)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------


class Operator(TimestampMixin, Base):
    """A CRM agent (or supervisor) authenticated with username + password."""

    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(ENUM_LENGTH),
        nullable=False,
        default=OperatorRole.OPERATOR.value,
        server_default=OperatorRole.OPERATOR.value,
    )
    shift_status: Mapped[str] = mapped_column(
        String(ENUM_LENGTH),
        nullable=False,
        default=ShiftStatus.OFFLINE.value,
        server_default=ShiftStatus.OFFLINE.value,
    )
    is_online_ws: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_target_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )
    # -- CONTRACTS_V2.md §2.3: admin-managed account flags ------------------
    #: Soft-disable switch: a blocked account keeps its history but may not log in.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Simultaneous-chat limit; ``NULL`` means "no per-operator cap".
    max_chats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Per-operator permissions granted from the admin panel.
    can_delete_messages: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    can_export: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: ``#RRGGBB`` avatar colour shown in the roster/chat list.
    avatar_color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    assigned_chats: Mapped[list[GroupChat]] = relationship(
        "GroupChat",
        back_populates="assigned_operator",
        foreign_keys="GroupChat.assigned_operator_id",
        passive_deletes=True,
    )
    sent_messages: Mapped[list[GroupMessage]] = relationship(
        "GroupMessage",
        back_populates="sender_operator",
        foreign_keys="GroupMessage.sender_operator_id",
        passive_deletes=True,
    )
    sla_incidents: Mapped[list[SLAIncident]] = relationship(
        "SLAIncident",
        back_populates="assigned_operator",
        foreign_keys="SLAIncident.assigned_operator_id",
        passive_deletes=True,
    )
    tx_lookups: Mapped[list[TxLookupAudit]] = relationship(
        "TxLookupAudit",
        back_populates="operator",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    #: Reactions this operator placed. The FK is ``SET NULL`` on delete, so the
    #: rows are not deleted with the operator (a hard-deleted account must keep
    #: its counters) — hence no ``delete-orphan`` here.
    reactions: Mapped[list[MessageReaction]] = relationship(
        "MessageReaction",
        back_populates="operator",
        foreign_keys="MessageReaction.operator_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(_check_values("role", OperatorRole), name="role_valid"),
        CheckConstraint(_check_values("shift_status", ShiftStatus), name="shift_status_valid"),
        CheckConstraint("sla_target_seconds > 0", name="sla_target_positive"),
        CheckConstraint("max_chats IS NULL OR max_chats >= 0", name="max_chats_non_negative"),
        CheckConstraint(
            "avatar_color IS NULL OR length(avatar_color) = 7",
            name="avatar_color_length",
        ),
    )

    @validates("role")
    def _validate_role(self, key: str, value: Any) -> Any:
        return enum_value(value, OperatorRole)

    @validates("shift_status")
    def _validate_shift_status(self, key: str, value: Any) -> Any:
        return enum_value(value, ShiftStatus)

    @validates("avatar_color")
    def _validate_avatar_color(self, key: str, value: Any) -> Any:
        """Normalise ``#RRGGBB`` (upper-case); ``None``/empty clears the colour.

        Accepts the 6-digit form with or without ``#`` and the 3-digit shorthand
        (``abc`` -> ``#AABBCC``). Anything else raises: a colour that is not a hex
        triple would render as a transparent avatar and silently break the UI.
        """
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        if candidate.startswith("#"):
            candidate = candidate[1:]
        # 3-digit shorthand -> 6-digit, which the column (and the CHECK) expects.
        if len(candidate) == 3:
            candidate = "".join(piece * 2 for piece in candidate)
        if len(candidate) != 6 or any(char not in _HEX_DIGITS for char in candidate):
            raise ValueError(
                f"avatar_color must be #RRGGBB or #RGB, got {value!r}"
            )
        return f"#{candidate.upper()}"

    @property
    def is_supervisor(self) -> bool:
        """``True`` for supervisors (full audit visibility, may assign anyone)."""
        return self.role == OperatorRole.SUPERVISOR.value

    @property
    def is_admin(self) -> bool:
        """``True`` for administrators (CONTRACTS_V2.md §3.3: full access)."""
        return self.role == OperatorRole.ADMIN.value

    @property
    def is_privileged(self) -> bool:
        """``True`` for any account allowed into the admin panel (admin or supervisor)."""
        return self.role in (OperatorRole.ADMIN.value, OperatorRole.SUPERVISOR.value)

    @property
    def initials(self) -> str:
        """One-or-two letter initials of :attr:`display_name` (avatar placeholder)."""
        parts = [part for part in str(self.display_name or "").split() if part]
        if not parts:
            return str(self.username or "?")[:2].upper()
        return "".join(part[0] for part in parts[:2]).upper()

    @property
    def sla_target(self) -> int:
        """Per-operator SLA target in seconds, falling back to the global 300 s."""
        return self.sla_target_seconds or 300

    def __repr__(self) -> str:
        return (
            f"<Operator id={self.id!r} username={self.username!r} "
            f"role={self.role!r} shift={self.shift_status!r}>"
        )


# ---------------------------------------------------------------------------
# group_chats
# ---------------------------------------------------------------------------


class GroupChat(TimestampMixin, Base):
    """A monitored Telegram group chat / supergroup belonging to one merchant."""

    __tablename__ = "group_chats"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merchant_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    assigned_operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assigned_operator: Mapped[Operator | None] = relationship(
        "Operator",
        back_populates="assigned_chats",
        foreign_keys=[assigned_operator_id],
        lazy="selectin",
    )
    messages: Mapped[list[GroupMessage]] = relationship(
        "GroupMessage",
        back_populates="chat",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="GroupMessage.created_at",
    )
    sla_incidents: Mapped[list[SLAIncident]] = relationship(
        "SLAIncident",
        back_populates="chat",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SLAIncident.created_at",
    )

    __table_args__ = (
        UniqueConstraint("tg_chat_id", name="uq_group_chats_tg_chat_id"),
        CheckConstraint("unread_count >= 0", name="unread_non_negative"),
        Index("ix_group_chats_assigned_last_message", "assigned_operator_id", "last_message_at"),
    )

    @property
    def display_assignment(self) -> str:
        """Human-readable assignment label for the chat list header."""
        if self.assigned_operator is not None:
            return self.assigned_operator.display_name
        if self.assigned_operator_id is not None:
            return f"Operator #{self.assigned_operator_id}"
        return "Unassigned"

    @property
    def assigned_operator_name(self) -> str | None:
        """Display name of the assignee, or ``None`` when unassigned."""
        if self.assigned_operator is not None:
            return self.assigned_operator.display_name
        if self.assigned_operator_id is not None:
            return f"Operator #{self.assigned_operator_id}"
        return None

    def __repr__(self) -> str:
        return (
            f"<GroupChat id={self.id!r} tg_chat_id={self.tg_chat_id!r} "
            f"title={self.title!r} unread={self.unread_count!r}>"
        )


# ---------------------------------------------------------------------------
# group_messages
# ---------------------------------------------------------------------------


class GroupMessage(TimestampMixin, Base):
    """One message inside a :class:`GroupChat` (inbound or outbound)."""

    __tablename__ = "group_messages"

    # BigInteger + Identity: the CRM id is mirrored back to the Telethon worker and
    # grows much faster than the other tables, so keep 64 bits of headroom.
    # ``with_variant(Integer, "sqlite")`` keeps the automated tests runnable on
    # in-memory SQLite, which cannot autoincrement a BIGINT primary key.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), Identity(), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_chats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tg_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_type: Mapped[str] = mapped_column(String(ENUM_LENGTH), nullable=False)
    sender_operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Telegram ``@username`` of an inbound sender. Bots are replied to by mentioning it,
    #: so the UI needs it to build "@usernamе …" replies (CONTRACTS_V2 §2.4).
    sender_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reply_to_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("group_messages.id", ondelete="SET NULL"), nullable=True
    )
    reply_to_tg_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_deleted_in_tg: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where the deletion originated (CONTRACTS_V5 §1.2): ``CRM`` or ``TELEGRAM``.
    #: ``NULL`` means "not deleted". Kept in lock-step with :attr:`is_deleted_in_tg`
    #: by ``ck_group_messages_deleted_source_consistent``: the flag and its source
    #: must never disagree, otherwise the grey-trace rendering cannot tell whether
    #: a tombstone was produced by us or mirrored from Telegram.
    deleted_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sla_incident_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sla_incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_status: Mapped[str] = mapped_column(
        String(ENUM_LENGTH),
        nullable=False,
        default=DeliveryStatus.SENT.value,
        server_default=DeliveryStatus.SENT.value,
    )
    client_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Deterministic mock ``TxStatus`` attached to seeded BOT alerts so the seeded
    #: transcripts stay consistent with what the mock admin adapters report.
    tx_status: Mapped[str | None] = mapped_column(String(ENUM_LENGTH), nullable=True)
    # -- CONTRACTS_V2.md §2.4: media support --------------------------------
    #: Media kind; ``TEXT`` for everything without an :class:`MediaAttachment`.
    kind: Mapped[str] = mapped_column(
        String(SHORT_ENUM_LENGTH),
        nullable=False,
        default=MessageKind.TEXT.value,
        server_default=MessageKind.TEXT.value,
    )
    #: Caption of the media (Telegram keeps it separate from the message text).
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``"Original channel — @username"`` style provenance label for forwards.
    forwarded_from: Mapped[str | None] = mapped_column(String(255), nullable=True)

    chat: Mapped[GroupChat] = relationship(
        "GroupChat", back_populates="messages", foreign_keys=[chat_id]
    )
    sender_operator: Mapped[Operator | None] = relationship(
        "Operator",
        back_populates="sent_messages",
        foreign_keys=[sender_operator_id],
        lazy="selectin",
    )
    #: Files carried by this message. ``lazy="selectin"`` matches the rest of the
    #: module: a message list is always rendered together with its attachments.
    attachments: Mapped[list[MediaAttachment]] = relationship(
        "MediaAttachment",
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="MediaAttachment.id",
    )
    #: Reactions placed on this message (CRM and Telegram alike).
    reactions: Mapped[list[MessageReaction]] = relationship(
        "MessageReaction",
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="MessageReaction.id",
    )
    #: Edit history mirrored from Telegram (CONTRACTS_V3 §2.1). CRM-side edits
    #: never write here, so the list is empty for messages only edited by us.
    #: ``passive_deletes`` lets the ``ON DELETE CASCADE`` of the FK do the work
    #: instead of loading and deleting every row in Python.
    edits: Mapped[list["MessageEdit"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageEdit.created_at",
        lazy="selectin",
        passive_deletes=True,
    )
    # Self-referential quote chain. ``lazy="selectin"`` is intentionally NOT used
    # here: the API only needs the quoted ids, not the whole recursive thread.
    reply_to: Mapped[GroupMessage | None] = relationship(
        "GroupMessage",
        remote_side=[id],
        foreign_keys=[reply_to_id],
        back_populates="replies",
    )
    replies: Mapped[list[GroupMessage]] = relationship(
        "GroupMessage",
        foreign_keys=[reply_to_id],
        back_populates="reply_to",
        passive_deletes=True,
    )
    # ``group_messages.sla_incident_id`` and ``sla_incidents.resolved_by_message_id``
    # point at each other, so these two relationships form a *mutually dependent*
    # many-to-one pair. ``back_populates`` is illegal here (SQLAlchemy rejects two
    # same-direction sides), therefore each side is declared independently and
    # ``post_update=True`` breaks the INSERT/UPDATE cycle on flush.
    sla_incident: Mapped[SLAIncident | None] = relationship(
        "SLAIncident",
        foreign_keys=[sla_incident_id],
        post_update=True,
    )

    __table_args__ = (
        CheckConstraint(_check_values("sender_type", MessageSenderType), name="sender_type_valid"),
        CheckConstraint(
            _check_values("delivery_status", DeliveryStatus), name="delivery_status_valid"
        ),
        # ``length()`` exists in both PostgreSQL and SQLite (``char_length`` is
        # PostgreSQL-only), so this CHECK also holds in the test database.
        CheckConstraint("length(text) <= 4096", name="text_length_valid"),
        CheckConstraint(
            "tx_status IS NULL OR " + _check_values("tx_status", TxStatus), name="tx_status_valid"
        ),
        CheckConstraint(_check_values("kind", MessageKind), name="kind_valid"),
        CheckConstraint(
            "caption IS NULL OR length(caption) <= 1024", name="caption_length_valid"
        ),
        # -- CONTRACTS_V5 §1.2: deletion provenance -------------------------
        # The vocabulary is closed: a tombstone is either ours (``CRM``) or mirrored
        # from Telegram; ``NULL`` means the row is alive.
        CheckConstraint(
            "deleted_source IS NULL OR " + _check_values("deleted_source", DeletedSource),
            name="deleted_source_valid",
        ),
        # The flag and its provenance are two halves of one fact (CONTRACTS_V5 §8.2):
        # ``is_deleted_in_tg = true`` requires a source, and a live row must not carry
        # one. The migration backfills ``TELEGRAM`` on legacy tombstones before this
        # constraint is created, otherwise the ALTER would be rejected.
        CheckConstraint(
            "(is_deleted_in_tg = false AND deleted_source IS NULL) "
            "OR (is_deleted_in_tg = true AND deleted_source IS NOT NULL)",
            name="deleted_source_consistent",
        ),
        UniqueConstraint("client_nonce", name="uq_group_messages_client_nonce"),
        Index("ix_group_messages_chat_created", "chat_id", "created_at"),
        Index("ix_group_messages_chat_tg_msg", "chat_id", "tg_msg_id"),
    )

    @validates("sender_type")
    def _validate_sender_type(self, key: str, value: Any) -> Any:
        return enum_value(value, MessageSenderType)

    @validates("delivery_status")
    def _validate_delivery_status(self, key: str, value: Any) -> Any:
        return enum_value(value, DeliveryStatus)

    @validates("tx_status")
    def _validate_tx_status(self, key: str, value: Any) -> Any:
        return enum_value(value, TxStatus)

    @validates("kind")
    def _validate_kind(self, key: str, value: Any) -> Any:
        return enum_value(value, MessageKind)

    @property
    def is_outgoing(self) -> bool:
        """``True`` when a CRM operator authored this message."""
        return self.sender_type == MessageSenderType.OPERATOR.value

    @property
    def is_deleted(self) -> bool:
        """``True`` when the message was deleted on the Telegram side."""
        return bool(self.is_deleted_in_tg or self.deleted_at is not None)

    @property
    def message_kind(self) -> str:
        """Effective media kind: derived from the attachments when ``kind`` is ``TEXT``."""
        if self.kind and self.kind != MessageKind.TEXT.value:
            return self.kind
        for attachment in self.attachments or ():
            kind = getattr(attachment, "kind", None)
            if kind:
                return str(kind)
        return MessageKind.TEXT.value

    @property
    def is_media(self) -> bool:
        """``True`` when the message carries at least one attachment."""
        return bool(self.attachments)

    @property
    def preview_text(self) -> str:
        """One-line list preview: caption for media, else the (flattened) text."""
        raw = (self.caption or "").strip() or (self.text or "").strip()
        if not raw:
            return f"[{self.message_kind}]"
        flattened = " ".join(raw.split())
        return flattened if len(flattened) <= 120 else f"{flattened[:117]}…"

    def __repr__(self) -> str:
        preview = (self.text or "")[:24].replace("\n", " ")
        return (
            f"<GroupMessage id={self.id!r} chat_id={self.chat_id!r} "
            f"sender={self.sender_type!r} tg_msg_id={self.tg_msg_id!r} text={preview!r}>"
        )


# ---------------------------------------------------------------------------
# sla_incidents
# ---------------------------------------------------------------------------


class SLAIncident(Base):
    """One SLA clock started by an unanswered client message."""

    __tablename__ = "sla_incidents"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("group_chats.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_messages.id", ondelete="CASCADE"), nullable=False
    )
    assigned_operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(ENUM_LENGTH),
        nullable=False,
        default=SlaIncidentStatus.PENDING.value,
        server_default=SlaIncidentStatus.PENDING.value,
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("group_messages.id", ondelete="SET NULL"), nullable=True
    )
    response_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    chat: Mapped[GroupChat] = relationship(
        "GroupChat", back_populates="sla_incidents", foreign_keys=[chat_id]
    )
    message: Mapped[GroupMessage] = relationship(
        "GroupMessage", foreign_keys=[message_id], lazy="selectin"
    )
    # Declared independently of ``GroupMessage.sla_incident`` (see the comment
    # there): the two FKs reference each other, so no ``back_populates``.
    resolved_message: Mapped[GroupMessage | None] = relationship(
        "GroupMessage",
        foreign_keys=[resolved_by_message_id],
        post_update=True,
    )
    assigned_operator: Mapped[Operator | None] = relationship(
        "Operator",
        back_populates="sla_incidents",
        foreign_keys=[assigned_operator_id],
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(_check_values("status", SlaIncidentStatus), name="status_valid"),
        CheckConstraint(
            "response_time_seconds IS NULL OR response_time_seconds >= 0",
            name="response_time_non_negative",
        ),
        Index("ix_sla_incidents_status_chat", "status", "chat_id"),
        Index("ix_sla_incidents_deadline", "deadline_at"),
    )

    @validates("status")
    def _validate_status(self, key: str, value: Any) -> Any:
        return enum_value(value, SlaIncidentStatus)

    # -- helpers ---------------------------------------------------------

    @property
    def remaining_seconds(self) -> int:
        """Seconds left until :attr:`deadline_at` (negative once overdue)."""
        if self.deadline_at is None:  # pragma: no cover - column is NOT NULL
            return 0
        deadline = self.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return int((deadline - utcnow()).total_seconds())

    @property
    def is_pending(self) -> bool:
        """``True`` while the incident still awaits an operator reply."""
        return self.status == SlaIncidentStatus.PENDING.value

    @property
    def is_resolved(self) -> bool:
        """``True`` once an operator reply resolved the incident."""
        return self.status == SlaIncidentStatus.RESOLVED.value

    @property
    def is_overdue(self) -> bool:
        """``True`` while a PENDING incident has passed its deadline."""
        return self.is_pending and self.remaining_seconds < 0

    @property
    def assigned_operator_name(self) -> str | None:
        """Display name of the assigned operator, with an id-based fallback."""
        if self.assigned_operator is not None:
            return self.assigned_operator.display_name
        if self.assigned_operator_id is not None:
            return f"Operator #{self.assigned_operator_id}"
        return None

    def __repr__(self) -> str:
        return (
            f"<SLAIncident id={self.id!r} chat_id={self.chat_id!r} "
            f"status={self.status!r} deadline_at={self.deadline_at!r}>"
        )


# ---------------------------------------------------------------------------
# tx_lookup_audit
# ---------------------------------------------------------------------------


class TxLookupAudit(Base):
    """Audit row written for every transaction lookup against an admin panel."""

    __tablename__ = "tx_lookup_audit"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    operator_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tx_id: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_panel: Mapped[str] = mapped_column(String(ENUM_LENGTH), nullable=False)
    found: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    response_status: Mapped[str | None] = mapped_column(String(ENUM_LENGTH), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    lookup_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    operator: Mapped[Operator] = relationship(
        "Operator", back_populates="tx_lookups", foreign_keys=[operator_id], lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint(_check_values("admin_panel", TxAdminPanel), name="admin_panel_valid"),
        CheckConstraint(
            "response_status IS NULL OR " + _check_values("response_status", TxStatus),
            name="response_status_valid",
        ),
        CheckConstraint("lookup_ms >= 0", name="lookup_ms_non_negative"),
        Index("ix_tx_lookup_audit_tx_id", "tx_id"),
        Index("ix_tx_lookup_audit_operator_created", "operator_id", "created_at"),
    )

    @validates("admin_panel")
    def _validate_admin_panel(self, key: str, value: Any) -> Any:
        return enum_value(value, TxAdminPanel)

    @validates("response_status")
    def _validate_response_status(self, key: str, value: Any) -> Any:
        return enum_value(value, TxStatus)

    @property
    def operator_username(self) -> str | None:
        """Username of the operator that ran the lookup (for audit responses)."""
        return self.operator.username if self.operator is not None else None

    def __repr__(self) -> str:
        return (
            f"<TxLookupAudit id={self.id!r} tx_id={self.tx_id!r} "
            f"panel={self.admin_panel!r} found={self.found!r}>"
        )


# ---------------------------------------------------------------------------
# media_attachments  (CONTRACTS_V2.md §2.1)
# ---------------------------------------------------------------------------


class MediaAttachment(Base):
    """One stored file belonging to a :class:`GroupMessage`.

    ``storage_path`` is **relative to** ``settings.media_root`` so the media tree
    can be relocated (or mounted from a volume) without rewriting rows; the
    REST layer joins it back with ``media_root`` before touching the disk. Paths
    are always POSIX-style (``chats/{chat_id}/{message_id}/{name}``) so a row is
    portable between Windows (dev) and Linux (production).

    There is no ``updated_at``: an attachment is written once and never
    modified — only deleted together with its message.
    """

    __tablename__ = "media_attachments"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: 64-bit to match ``group_messages.id`` (``with_variant`` keeps SQLite usable).
    message_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("group_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(SHORT_ENUM_LENGTH),
        nullable=False,
        default=MessageKind.DOCUMENT.value,
        server_default=MessageKind.DOCUMENT.value,
    )
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Pixel dimensions — only meaningful for images/stickers/video thumbnails.
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Playback length in seconds — voice/video/audio.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Path **relative to** ``settings.media_root`` (CONTRACTS_V2.md §2.1).
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Telegram file reference, so the worker can re-download instead of re-uploading.
    tg_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Relative path of the generated preview (images/video only).
    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    message: Mapped[GroupMessage] = relationship(
        "GroupMessage", back_populates="attachments", foreign_keys=[message_id]
    )

    __table_args__ = (
        CheckConstraint(_check_values("kind", MessageKind), name="kind_valid"),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="size_bytes_non_negative"
        ),
        CheckConstraint(
            "width IS NULL OR width >= 0", name="width_non_negative"
        ),
        CheckConstraint(
            "height IS NULL OR height >= 0", name="height_non_negative"
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="duration_seconds_non_negative",
        ),
        UniqueConstraint(
            "message_id", "storage_path", name="uq_media_attachments_message_path"
        ),
        Index("ix_media_attachments_message_id", "message_id"),
    )

    @validates("kind")
    def _validate_kind(self, key: str, value: Any) -> Any:
        return enum_value(value, MessageKind)

    @validates("storage_path", "thumbnail_path")
    def _validate_storage_path(self, key: str, value: Any) -> Any:
        """Normalise a stored path to a relative POSIX path (defensive)."""
        if value is None:
            return None
        candidate = str(value).strip().replace("\\", "/")
        while candidate.startswith("./"):
            candidate = candidate[2:]
        return candidate.lstrip("/") or None

    @property
    def is_image(self) -> bool:
        """``True`` for anything with pixel dimensions / an image MIME type."""
        return self.kind in (
            MessageKind.PHOTO.value,
            MessageKind.STICKER.value,
        ) or str(self.mime_type or "").startswith("image/")

    @property
    def is_playable(self) -> bool:
        """``True`` for media that has a duration (voice/video/audio)."""
        return self.kind in (
            MessageKind.VOICE.value,
            MessageKind.VIDEO.value,
            MessageKind.AUDIO.value,
        )

    @property
    def has_thumbnail(self) -> bool:
        """``True`` when a preview file is recorded for this attachment."""
        return bool(self.thumbnail_path)

    def __repr__(self) -> str:
        return (
            f"<MediaAttachment id={self.id!r} message_id={self.message_id!r} "
            f"kind={self.kind!r} path={self.storage_path!r}>"
        )


# ---------------------------------------------------------------------------
# message_reactions  (CONTRACTS_V2.md §2.2)
# ---------------------------------------------------------------------------


class MessageReaction(Base):
    """One emoji reaction on a :class:`GroupMessage`.

    Two mutually exclusive uniqueness rules (CONTRACTS_V2.md §2.2):

    * a CRM operator may hold **one** reaction per message —
      ``UNIQUE(message_id, operator_id, emoji) WHERE operator_id IS NOT NULL``;
    * a Telegram user is likewise counted once —
      ``UNIQUE(message_id, tg_user_id, emoji) WHERE tg_user_id IS NOT NULL``.

    They are partial ``Index`` objects (``postgresql_where`` /
    ``sqlite_where``) because the contract's rule is literally "where … NOT
    NULL", and because a plain ``UniqueConstraint`` would collide on the
    all-``NULL`` Telegram side. PostgreSQL and SQLite both support partial
    indexes, so the same DDL guards production and the test database.
    """

    __tablename__ = "message_reactions"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: 64-bit to match ``group_messages.id`` (``with_variant`` keeps SQLite usable).
    message_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("group_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: ``NULL`` when the reaction came from Telegram (identified by ``tg_user_id``).
    operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True
    )
    emoji: Mapped[str] = mapped_column(String(SHORT_ENUM_LENGTH), nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: ``CRM`` or ``TELEGRAM`` — who originated the reaction.
    source: Mapped[str] = mapped_column(
        String(SHORT_ENUM_LENGTH),
        nullable=False,
        default=REACTION_SOURCE_CRM,
        server_default=REACTION_SOURCE_CRM,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    message: Mapped[GroupMessage] = relationship(
        "GroupMessage", back_populates="reactions", foreign_keys=[message_id]
    )
    operator: Mapped[Operator | None] = relationship(
        "Operator",
        back_populates="reactions",
        foreign_keys=[operator_id],
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(_check_values("emoji", ReactionEmoji), name="emoji_valid"),
        CheckConstraint(_check_values("source", ReactionSource), name="source_valid"),
        CheckConstraint(
            "source = 'TELEGRAM' OR operator_id IS NOT NULL", name="crm_source_needs_operator"
        ),
        # One reaction per operator per message (the API swaps the emoji in place).
        Index(
            "uq_message_reactions_message_operator_emoji",
            "message_id",
            "operator_id",
            "emoji",
            unique=True,
            postgresql_where=text("operator_id IS NOT NULL"),
            sqlite_where=text("operator_id IS NOT NULL"),
        ),
        # One reaction per Telegram user per message.
        Index(
            "uq_message_reactions_message_tg_user_emoji",
            "message_id",
            "tg_user_id",
            "emoji",
            unique=True,
            postgresql_where=text("tg_user_id IS NOT NULL"),
            sqlite_where=text("tg_user_id IS NOT NULL"),
        ),
        Index("ix_message_reactions_message_emoji", "message_id", "emoji"),
        Index("ix_message_reactions_operator_id", "operator_id"),
    )

    @validates("emoji")
    def _validate_emoji(self, key: str, value: Any) -> Any:
        return enum_value(value, ReactionEmoji)

    @validates("source")
    def _validate_source(self, key: str, value: Any) -> Any:
        """Normalise to ``CRM``/``TELEGRAM`` (accepts any case)."""
        if value is None:
            return REACTION_SOURCE_CRM
        candidate = str(getattr(value, "value", value)).strip().upper()
        if candidate not in (REACTION_SOURCE_CRM, REACTION_SOURCE_TELEGRAM):
            raise LookupError(
                f"invalid reaction source: {value!r} (valid: {REACTION_SOURCE_CRM}, "
                f"{REACTION_SOURCE_TELEGRAM})"
            )
        return candidate

    @property
    def is_crm(self) -> bool:
        """``True`` when a CRM operator placed the reaction."""
        return self.source == REACTION_SOURCE_CRM

    @property
    def is_telegram(self) -> bool:
        """``True`` when the reaction was mirrored from Telegram."""
        return self.source == REACTION_SOURCE_TELEGRAM

    def __repr__(self) -> str:
        return (
            f"<MessageReaction id={self.id!r} message_id={self.message_id!r} "
            f"operator_id={self.operator_id!r} tg_user_id={self.tg_user_id!r} "
            f"source={self.source!r}>"
        )


# ---------------------------------------------------------------------------
# telegram_accounts  (CONTRACTS_V2.md §2.5)
# ---------------------------------------------------------------------------


class TelegramAccount(TimestampMixin, Base):
    """One Telegram account the worker may drive (multi-account support).

    ``session_string`` and ``api_hash`` are stored **encrypted** by
    ``services/admin.py`` (Fernet / ``settings.secret_box_key``); the model keeps
    them as opaque ``Text``/``String(64)`` so a rotation of the key never changes
    the schema. The API never returns them in clear — the admin router masks the
    session (CONTRACTS_V2.md §3.3).
    """

    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: Human label shown in the admin panel, e.g. «Основной аккаунт».
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: **Encrypted** Telethon ``StringSession`` (never stored in clear).
    #: ``Text``: a raw Telethon string session is 350-400+ characters and Fernet
    #: adds ~33% on top, so no fixed width could hold it.
    session_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: **Encrypted** ``api_hash`` (never stored in clear).
    #: ``String(512)`` rather than the contract's 64: a Fernet token of a
    #: 32-character hex hash is ~120 characters, and 512 leaves headroom for a
    #: key rotation or a wider plaintext without a schema change.
    api_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("label", name="uq_telegram_accounts_label"),
        CheckConstraint(
            "api_id IS NULL OR api_id > 0", name="api_id_positive"
        ),
        Index("ix_telegram_accounts_tg_user_id", "tg_user_id"),
        Index("ix_telegram_accounts_is_active", "is_active"),
    )

    @validates("label")
    def _validate_label(self, key: str, value: Any) -> Any:
        """Trim the label and reject an empty one (it is the natural key)."""
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("telegram account label must not be empty")
        return candidate

    @property
    def has_session(self) -> bool:
        """``True`` once a (encrypted) session string has been stored."""
        return bool(self.session_string)

    @property
    def display_handle(self) -> str:
        """``@username`` when known, else the phone, else the label."""
        if self.tg_username:
            return f"@{self.tg_username.lstrip('@')}"
        return self.phone or self.label

    def __repr__(self) -> str:
        # The session string is never part of the representation (secret hygiene).
        return (
            f"<TelegramAccount id={self.id!r} label={self.label!r} "
            f"username={self.tg_username!r} active={self.is_active!r}>"
        )


# ---------------------------------------------------------------------------
# admin_audit_log  (CONTRACTS_V2.md §3.3 — «GET /api/admin/audit»)
# ---------------------------------------------------------------------------


class AuditEntry(Base):
    """One administrative action recorded by ``app/services/admin.py``.

    The journal is **append-only**: rows are never updated and never deleted, even
    when the operator, account or message they refer to disappears. That is why
    ``actor_id`` is ``ON DELETE SET NULL`` (the action still happened and must
    survive an account purge) and ``target_id`` carries no foreign key at all — an
    audit row must not block, or be blocked by, the lifecycle of its target.

    Only non-secret facts are stored in ``detail``: a change list for operator
    updates, the label for accounts. Passwords and Telegram secrets never reach
    this table.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: Administrator who performed the action; ``NULL`` for system/anonymous events.
    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True
    )
    #: One of :data:`AUDIT_ACTION_VALUES` (``create_operator``, ``reset_password``, …).
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``"operator"`` / ``"telegram_account"``; ``NULL`` when the action has no target.
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Human-readable, secret-free description of what changed.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    actor: Mapped[Operator | None] = relationship(
        "Operator",
        foreign_keys=[actor_id],
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(_check_values("action", _AuditAction), name="action_valid"),
        Index("ix_admin_audit_log_created_at", "created_at"),
        Index("ix_admin_audit_log_actor_created", "actor_id", "created_at"),
        Index("ix_admin_audit_log_target", "target_type", "target_id"),
    )

    @validates("action")
    def _validate_action(self, key: str, value: Any) -> Any:
        """Reject an unknown action: the journal must stay a closed vocabulary."""
        candidate = str(value or "").strip().lower()
        if candidate not in AUDIT_ACTION_VALUES:
            valid = ", ".join(AUDIT_ACTION_VALUES)
            raise LookupError(f"invalid audit action: {value!r} (valid: {valid})")
        return candidate

    @validates("target_type")
    def _validate_target_type(self, key: str, value: Any) -> Any:
        """Normalise the target discriminator to ``snake_case`` (``NULL`` allowed)."""
        if value is None:
            return None
        candidate = str(value).strip().lower()
        return candidate or None

    @property
    def actor_username(self) -> str | None:
        """Username of the acting administrator (``None`` once the row is gone)."""
        actor = self.actor
        return actor.username if actor is not None else None

    def __repr__(self) -> str:
        return (
            f"<AuditEntry id={self.id!r} action={self.action!r} "
            f"actor_id={self.actor_id!r} target={self.target_type!r}:{self.target_id!r}>"
        )


# ---------------------------------------------------------------------------
# message_edits  (CONTRACTS_V3 §2.2)
# ---------------------------------------------------------------------------


class MessageEdit(Base):
    """One previous version of a :class:`GroupMessage` text (CONTRACTS_V3 §2).

    Asymmetry is the whole point of the table (§2.1):

    * a ``TELEGRAM`` edit is mirrored here — the client may rewrite what they said,
      so the version we saw **before** the edit must survive;
    * a ``CRM`` edit is our own correction and is **never** recorded here (§5.3);
      the ``CRM`` member exists so the vocabulary stays closed and an accidental
      write is still a valid, queryable row rather than garbage.

    The row is immutable once written: there is no ``updated_at`` and no setter
    path — history is append-only, like :class:`AuditEntry`. ``operator_id`` is
    ``ON DELETE SET NULL`` so a purged account keeps the history intact, with
    ``actor_name`` as the human-readable fallback.
    """

    __tablename__ = "message_edits"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: 64-bit to match ``group_messages.id`` (``with_variant`` keeps SQLite usable).
    message_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("group_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: ``TELEGRAM`` or ``CRM`` — who changed the text (§2.2).
    source: Mapped[str] = mapped_column(String(EDIT_SOURCE_LENGTH), nullable=False)
    #: Text as it was before the edit (``NOT NULL``: an edit always replaces text).
    old_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Text as it became after the edit.
    new_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: CRM operator responsible for a ``CRM`` edit; ``NULL`` for Telegram edits.
    operator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True
    )
    #: Display name from Telegram (``actor_name``) or the operator, kept verbatim
    #: so the history still reads correctly after the account is deleted.
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    message: Mapped[GroupMessage] = relationship(
        "GroupMessage",
        back_populates="edits",
        foreign_keys=[message_id],
    )
    operator: Mapped[Operator | None] = relationship(
        "Operator",
        foreign_keys=[operator_id],
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(_check_values("source", _EditSource), name="source_valid"),
        # The history is always read as "versions of one message, oldest first",
        # which is exactly the index the contract asks for (§2.2).
        Index("ix_message_edits_message_created", "message_id", "created_at"),
    )

    @validates("source")
    def _validate_source(self, key: str, value: Any) -> Any:
        """Normalise to ``TELEGRAM``/``CRM`` (accepts any case, rejects the rest)."""
        return enum_value(value, _EditSource)

    @property
    def is_telegram(self) -> bool:
        """``True`` when the edit was mirrored from Telegram (the audited case)."""
        return self.source == _EditSource.TELEGRAM.value

    @property
    def is_crm(self) -> bool:
        """``True`` when a CRM operator made the edit."""
        return self.source == _EditSource.CRM.value

    @property
    def edited_by(self) -> str | None:
        """Best label for "who edited": ``actor_name``, else the linked operator."""
        if self.actor_name:
            return self.actor_name
        return self.operator.display_name if self.operator is not None else None

    def __repr__(self) -> str:
        old = (self.old_text or "")[:24].replace("\n", " ")
        return (
            f"<MessageEdit id={self.id!r} message_id={self.message_id!r} "
            f"source={self.source!r} old_text={old!r}>"
        )


# ---------------------------------------------------------------------------
# app_settings  (CONTRACTS_V5 §2.1)
# ---------------------------------------------------------------------------


class AppSettings(Base):
    """Process-wide editable settings, stored in a **single** row ``id = 1`` (§2.1).

    A one-row table (not a key/value store) because the contract's settings are a
    closed, typed set: a row lets every value carry a real column type, a ``NOT
    NULL`` guarantee and a ``CHECK``, which a ``settings(key, value TEXT)`` table
    could not enforce. :meth:`get_or_create` is the only supported way to obtain
    the row, so callers never have to care whether it exists yet.

    There is no ``created_at``: the row is bootstrapped with defaults on first
    read, so "when it was created" is meaningless — :attr:`updated_at` (plus
    :attr:`updated_by`) is the audit field that matters.
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    #: Master switch (§2.3): ``False`` stops new SLA incidents from being opened.
    sla_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Global SLA target in seconds; a per-operator value still wins (§2.3).
    sla_target_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_SLA_TARGET_SECONDS,
        server_default=str(DEFAULT_SLA_TARGET_SECONDS),
    )
    #: ``True`` (default) enables the V5 rule «ответ оператора сбрасывает клок» (§3.2).
    sla_reset_on_operator_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
    #: Username of the administrator who last changed the row (``NULL`` for the seed).
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # §2.1: the window is bounded on both sides; 60 s is the shortest clock that
        # can still be acted upon, 86400 s (24 h) the longest that is still an SLA.
        CheckConstraint(
            f"sla_target_seconds BETWEEN {SLA_TARGET_SECONDS_MIN} "
            f"AND {SLA_TARGET_SECONDS_MAX}",
            name="sla_target",
        ),
        # The table is a singleton: a second row would make "the" settings ambiguous.
        CheckConstraint(f"id = {APP_SETTINGS_ID}", name="singleton_row"),
    )

    @validates("sla_target_seconds")
    def _validate_sla_target_seconds(self, key: str, value: Any) -> Any:
        """Reject an out-of-range target in Python, before the ``CHECK`` fires.

        Duplicating the bound is deliberate: a bad PATCH then fails with a clear
        message in the router instead of an ``IntegrityError`` from the flush.
        """
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"sla_target_seconds must be an integer, got {value!r}") from None
        if not SLA_TARGET_SECONDS_MIN <= seconds <= SLA_TARGET_SECONDS_MAX:
            raise ValueError(
                f"sla_target_seconds must be between {SLA_TARGET_SECONDS_MIN} and "
                f"{SLA_TARGET_SECONDS_MAX}, got {seconds}"
            )
        return seconds

    @validates("updated_by")
    def _validate_updated_by(self, key: str, value: Any) -> Any | None:
        """Trim the actor name; an empty string is stored as ``NULL``."""
        if value is None:
            return None
        candidate = str(value).strip()
        return candidate or None

    # -- helpers ---------------------------------------------------------

    @classmethod
    def get_or_create(cls, db: Any) -> AppSettings:
        """Return the singleton row, inserting the documented defaults on first use.

        ``db`` is a **synchronous** SQLAlchemy ``Session`` (the service layer calls
        this through ``run_sync`` or from a sync context); an ``AsyncSession`` is
        rejected explicitly so a missing ``await`` cannot silently degrade into an
        ``AttributeError`` deep inside SQLAlchemy. The insert is committed by the
        caller: the method only stages the row so the surrounding unit of work
        stays atomic with the settings read that follows it.
        """
        existing = db.get(cls, APP_SETTINGS_ID)
        if existing is not None:
            return existing
        row = cls(
            id=APP_SETTINGS_ID,
            sla_enabled=True,
            sla_target_seconds=DEFAULT_SLA_TARGET_SECONDS,
            sla_reset_on_operator_reply=True,
        )
        db.add(row)
        db.flush()
        return row

    @property
    def sla_target_minutes(self) -> int:
        """SLA target rounded to whole minutes (human-readable admin panel view)."""
        return round(self.sla_target_seconds / 60)

    def __repr__(self) -> str:
        return (
            f"<AppSettings id={self.id!r} sla_enabled={self.sla_enabled!r} "
            f"sla_target_seconds={self.sla_target_seconds!r} "
            f"reset_on_reply={self.sla_reset_on_operator_reply!r}>"
        )


# ---------------------------------------------------------------------------
# quick_replies  (CONTRACTS_V5 §5.1)
# ---------------------------------------------------------------------------


class QuickReply(TimestampMixin, Base):
    """One editable canned response (CONTRACTS_V5 §5).

    The list used to live in ``MessageInput.tsx`` as a hard-coded constant; §5.4
    moves it into the database so supervisors can add, reorder and retire answers
    without a redeploy, and so every operator sees the same list (§5.3).

    ``shortcut`` is the natural key the seed and the API de-duplicate on: it is
    ``UNIQUE``, and a ``409`` from ``POST /api/quick-replies`` means this index
    was violated. Deleting an operator never deletes the reply — the FK is
    ``ON DELETE SET NULL`` — because a canned response outlives whoever typed it.
    """

    __tablename__ = "quick_replies"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    #: Short code used in the editor slash-menu, e.g. ``tx``; unique per system.
    shortcut: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    #: Caption shown in the reply list (§5.2 ``"Запросить TX ID"``).
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Exact text inserted into the composer; never blank (§5.1).
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Free-form grouping label, ``general`` by default.
    category: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=DEFAULT_QUICK_REPLY_CATEGORY,
        server_default=DEFAULT_QUICK_REPLY_CATEGORY,
    )
    #: Explicit ordering key (ascending); ties fall back to :attr:`id`.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Retired replies stay in the table (history, ``?all=true``) but are hidden.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operators.id", ondelete="SET NULL"), nullable=True
    )

    #: Declared independently (no ``back_populates``): ``Operator`` is not modified by
    #: CONTRACTS_V5, and the reverse collection would be a rarely-used list.
    created_by: Mapped[Operator | None] = relationship(
        "Operator",
        foreign_keys=[created_by_id],
        lazy="selectin",
    )

    __table_args__ = (
        # ``length(trim(...)) > 0`` is spelled out in SQL, not Python: the rule must
        # hold for raw ``INSERT``s too (the seed, ad-hoc SQL, the worker). ``trim``
        # and ``length`` exist in both PostgreSQL and SQLite, so the same DDL guards
        # production and the test database.
        #
        # CONTRACTS_V5 §5.1 spells this as ``btrim(body)``, the PostgreSQL name of the
        # same function. ``btrim`` does **not** exist in SQLite, and a ``CHECK`` is
        # compiled into ``CREATE TABLE`` (``create_all``) — so writing it literally
        # would make every SQLite fixture fail with
        # ``OperationalError: no such function: btrim`` while the rule itself stayed
        # unenforced. SQLite's ``trim(X)`` is the same two-argument ``btrim(X)``
        # (both strip ASCII spaces from both ends): the semantic is the contract's,
        # only the function name is portable.
        CheckConstraint("length(trim(body)) > 0", name="body_not_blank"),
        CheckConstraint("length(trim(shortcut)) > 0", name="shortcut_not_blank"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("length(trim(category)) > 0", name="category_not_blank"),
        # §5.1: ``UNIQUE (shortcut)`` and ``INDEX (is_active, sort_order)``. The unique
        # index is named explicitly (the column's ``unique=True`` would create an
        # unnamed ``uq_...`` constraint instead of the contract's ``ix_`` name); both
        # are requested lazily through ``Index`` so the CREATE runs at DDL time with
        # the rest of the table rather than back-filling an in-place ALTER.
        Index("ix_quick_replies_shortcut", "shortcut", unique=True),
        # Exactly the list query: «active replies, in display order».
        Index("ix_quick_replies_active_order", "is_active", "sort_order"),
    )

    @validates("shortcut")
    def _validate_shortcut(self, key: str, value: Any) -> str:
        """Trim the shortcut and reject an empty one (it is the natural key)."""
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("quick reply shortcut must not be empty")
        return candidate

    @validates("title")
    def _validate_title(self, key: str, value: Any) -> str:
        """Trim the title and reject an empty one (it is the only label in the UI)."""
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("quick reply title must not be empty")
        return candidate

    @validates("body")
    def _validate_body(self, key: str, value: Any) -> str:
        """Reject a blank body, keeping the text verbatim when it is not blank.

        Only the check is normalised: leading/trailing whitespace is meaningful in a
        message (an accidental extra blank line is harmless, a stripped indent is not),
        so the stored value keeps what the author typed (§5.2 ``422`` on blank).
        """
        candidate = "" if value is None else str(value)
        if not candidate.strip():
            raise ValueError("quick reply body must not be blank")
        return candidate

    @validates("category")
    def _validate_category(self, key: str, value: Any) -> str:
        """Normalise the category label; an omitted one falls back to ``general``."""
        if value is None:
            return DEFAULT_QUICK_REPLY_CATEGORY
        candidate = str(value).strip()
        return candidate or DEFAULT_QUICK_REPLY_CATEGORY

    @property
    def created_by_username(self) -> str | None:
        """Username of the author, or ``None`` when the account was purged."""
        author = self.created_by
        return author.username if author is not None else None

    @property
    def preview(self) -> str:
        """One-line preview of :attr:`body` (collapsed whitespace, 80 chars max)."""
        flattened = " ".join((self.body or "").split())
        return flattened if len(flattened) <= 80 else f"{flattened[:77]}…"

    def __repr__(self) -> str:
        return (
            f"<QuickReply id={self.id!r} shortcut={self.shortcut!r} "
            f"title={self.title!r} active={self.is_active!r}>"
        )

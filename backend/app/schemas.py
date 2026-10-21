"""Pydantic v2 request/response/WebSocket schemas for the TG-Desk CRM API.

Mirrors CONTRACTS.md §3 (REST) and §4 (WebSocket) exactly:

* snake_case field names, ``extra="forbid"`` on requests so typos fail loudly,
* every response datetime is serialised through :data:`UtcDatetime`, i.e. aware
  UTC ISO-8601 with a literal ``Z`` suffix (``2024-06-01T12:00:00Z``),
* every optional field declared in the contract is present with ``None`` defaults
  so the frontend can rely on a stable object shape,
* money is transported as a **decimal string** (``"1500.00"``), never a float.

The module has no circular dependency: it imports only ``app.enums`` and
``app.utils``. ORM objects are converted by the small ``from_*`` classmethods
below, which read attributes only (no SQLAlchemy imports), so schemas can also be
built from plain dicts / dataclasses in tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

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
)
from app.utils import clamp, format_decimal_2dp, iso_z, round_sla_percent

__all__ = [
    "AdminSettingsOut",
    "AdminSettingsUpdate",
    "AssignRequest",
    "BroadcastOut",
    "BroadcastRequest",
    "ChatListItem",
    "ChatListLastMessage",
    "ChatListOut",
    "ChatLoadState",
    "ChatSlaOut",
    "DeletedMessageOut",
    "ForwardRequest",
    "HeartbeatOut",
    "LoginRequest",
    "LoginResponse",
    "MessageEditListOut",
    "MessageEditOut",
    "MessageEditRequest",
    "MessageListOut",
    "MessageOut",
    "MessageUpdateOut",
    "OkOut",
    "OperatorListOut",
    "OperatorOut",
    "PersonalSlaStats",
    "QuickReplyIn",
    "QuickReplyListOut",
    "QuickReplyOut",
    "QuickReplyUpdate",
    "normalise_quick_reply_body",
    "normalise_quick_reply_category",
    "normalise_quick_reply_shortcut",
    "normalise_quick_reply_title",
    "ReadOut",
    "SearchHitOut",
    "SearchResponseOut",
    "SendMessageRequest",
    "ShiftStatusUpdate",
    "SlaSnapshot",
    "SystemInfoOut",
    "TelegramDeliveryInfo",
    "TypingRequest",
    "TypingResponse",
    "TxAuditItem",
    "TxAuditListOut",
    "TxLookupRequest",
    "TxLookupResponse",
    "TxPanelResult",
    "UtcDatetime",
    "WsBroadcastCommand",
    "WsChatAssignedData",
    "WsConnectedData",
    "WsEnvelope",
    "WsErrorData",
    "WsMessageDeletedData",
    "WsMessageStatusData",
    "WsNewMessageData",
    "WsOperatorPresenceData",
    "WsPongData",
    "WsShiftChangedData",
    "WsSlaResolvedData",
    "WsSlaUpdateData",
    "WsTypingLockData",
    "WsTypingUnlockData",
    "build_envelope",
    "dump_model",
    "utc_iso",
]

#: ``"1500.00"`` — decimal string with exactly two fractional digits.
AMOUNT_PATTERN: Final[str] = r"^-?\d{1,15}(\.\d{1,2})?$"

# ---------------------------------------------------------------------------
# Datetime plumbing — always emit ``...Z``
# ---------------------------------------------------------------------------


def utc_iso(value: datetime | None) -> str | None:
    """Return an aware-UTC ISO-8601 string with a ``Z`` suffix (or ``None``).

    Naive datetimes are treated as UTC, which is what asyncpg hands back for
    ``TIMESTAMP WITH TIME ZONE`` columns when the server session is UTC.
    """
    if value is None:
        return None
    if isinstance(value, str):  # already-serialised passthrough, defensive
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text = value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


#: ``Annotated`` datetime type used by **every** response/WS field.
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(utc_iso, return_type=str, when_used="json-unless-none"),
]

# ---------------------------------------------------------------------------
# SLA target bounds (CONTRACTS_V5 §2.1/§2.3)
# ---------------------------------------------------------------------------
# Declared here, above every model that references them, because ``OperatorCreateRequest``
# (CONTRACTS_V5 §2.3 per-operator override) is defined *before* the admin-settings block.
# Literals rather than imports from ``app.models`` keep this module a pure leaf (see the
# module docstring); the values are frozen by the contract and mirrored by the database
# ``CHECK``, so a drift would fail loudly on the first write.

#: Inclusive bounds of every SLA target: ``app_settings.sla_target_seconds`` and the
#: per-operator override share one window (§2.1).
SLA_TARGET_SECONDS_MIN: Final[int] = 60
SLA_TARGET_SECONDS_MAX: Final[int] = 86400
#: Target shown by the admin panel before anything was saved (§2.1 default ``1800``).
DEFAULT_SLA_TARGET_SECONDS: Final[int] = 1800


# ---------------------------------------------------------------------------
# Shared configuration
# ---------------------------------------------------------------------------


class _ORMModel(BaseModel):
    """Base for response models: read attributes, ignore undeclared extras."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=False,
    )


class _StrictModel(BaseModel):
    """Base for request models: reject unknown keys so clients fail loudly."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Operators (CONTRACTS.md §3.2)
# ---------------------------------------------------------------------------


class OperatorOut(_ORMModel):
    """``OperatorOut`` — the public projection of an ``operators`` row."""

    id: int
    username: str
    display_name: str
    role: str = OperatorRole.OPERATOR.value
    shift_status: str = ShiftStatus.OFFLINE.value
    is_online_ws: bool = False
    last_seen_at: UtcDatetime | None = None
    sla_target_seconds: int = 300
    created_at: UtcDatetime | None = None
    #: CONTRACTS_V2 §2.3 / CONTRACTS_V5 §1.3 — per-operator permissions. The frontend
    #: needs them to decide whether to render the delete (and export) controls at all;
    #: without them the UI cannot know why a DELETE would answer ``403``. Both are plain
    #: ``bool`` columns of ``operators`` and default to ``False`` (the contract default).
    can_delete_messages: bool = False
    can_export: bool = False

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return OperatorRole.coerce(value) or OperatorRole.OPERATOR.value

    @field_validator("shift_status")
    @classmethod
    def _validate_shift_status(cls, value: str) -> str:
        return ShiftStatus.coerce(value) or ShiftStatus.OFFLINE.value

    @property
    def is_supervisor(self) -> bool:
        """``True`` when this operator holds the supervisor role."""
        return self.role == OperatorRole.SUPERVISOR.value

    @classmethod
    def from_operator(cls, operator: Any) -> Self:
        """Build from an ``Operator`` ORM instance (or any attribute holder)."""
        return cls.model_validate(operator)


class OperatorListOut(_ORMModel):
    """``GET /api/shift/operators`` response envelope."""

    items: list[OperatorOut] = Field(default_factory=list)


class PersonalSlaStats(_ORMModel):
    """``PersonalSlaStats`` (CONTRACTS.md §3.2) — per-operator SLA summary."""

    operator_id: int
    shift_status: str = ShiftStatus.OFFLINE.value
    avg_response_seconds: int = 0
    resolved_count: int = 0
    open_count: int = 0
    overdue_count: int = 0
    sla_percent: float = 100.0
    sla_target_seconds: int = 300

    @field_validator("shift_status")
    @classmethod
    def _validate_shift_status(cls, value: str) -> str:
        return ShiftStatus.coerce(value) or ShiftStatus.OFFLINE.value

    @field_validator("sla_percent")
    @classmethod
    def _round_sla_percent(cls, value: float) -> float:
        """Round to one decimal and clamp into ``[0, 100]``."""
        return round(clamp(float(value), 0.0, 100.0), 1)

    @field_validator("avg_response_seconds", "resolved_count", "open_count", "overdue_count")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        return max(int(value), 0)

    @model_validator(mode="after")
    def _derive_percent(self) -> Self:
        """Derive ``sla_percent`` from counts when it was not supplied."""
        if "sla_percent" not in self.model_fields_set and self.resolved_count:
            on_time = max(self.resolved_count - self.overdue_count, 0)
            object.__setattr__(self, "sla_percent", round_sla_percent(self.resolved_count, on_time))
        return self


# ---------------------------------------------------------------------------
# Auth (CONTRACTS.md §3.1)
# ---------------------------------------------------------------------------


class LoginRequest(_StrictModel):
    """``POST /api/auth/login`` request body."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def _normalise_username(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not normalised:
            raise ValueError("username must not be blank")
        return normalised


class LoginResponse(_ORMModel):
    """``POST /api/auth/login`` 200 response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    operator: OperatorOut


# ---------------------------------------------------------------------------
# Shift (CONTRACTS.md §3.2)
# ---------------------------------------------------------------------------


class ShiftStatusUpdate(_StrictModel):
    """``POST /api/shift/status`` request body."""

    status: str = Field(min_length=1, max_length=24)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        coerced = ShiftStatus.coerce(value)
        if coerced is None:
            raise ValueError("status is required")
        return coerced


class HeartbeatOut(_ORMModel):
    """``POST /api/shift/heartbeat`` response."""

    ok: bool = True
    server_time: UtcDatetime


# ---------------------------------------------------------------------------
# SLA (CONTRACTS.md §3.3 embedded ``sla`` object)
# ---------------------------------------------------------------------------


class SlaSnapshot(_ORMModel):
    """The ``sla`` object embedded in :class:`ChatListItem` and ``SLA_UPDATE``."""

    status: str = SlaIncidentStatus.PENDING.value
    deadline_at: UtcDatetime | None = None
    remaining_seconds: int = 0
    is_overdue: bool = False

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        return SlaIncidentStatus.coerce(value) or SlaIncidentStatus.PENDING.value

    @model_validator(mode="after")
    def _derive_overdue(self) -> Self:
        """Derive ``is_overdue`` from the countdown when it was not supplied."""
        if "is_overdue" not in self.model_fields_set and self.deadline_at is not None:
            object.__setattr__(self, "is_overdue", self.remaining_seconds < 0)
        return self

    @classmethod
    def from_incident(cls, incident: Any) -> Self:
        """Build from an ``SLAIncident`` ORM row (uses its live properties)."""
        return cls(
            status=incident.status,
            deadline_at=incident.deadline_at,
            remaining_seconds=incident.remaining_seconds,
            is_overdue=incident.is_overdue,
        )


class ChatSlaOut(_ORMModel):
    """``SLA_UPDATE`` payload (CONTRACTS.md §4)."""

    chat_id: int
    incident_id: int
    status: str = SlaIncidentStatus.PENDING.value
    deadline_at: UtcDatetime | None = None
    remaining_seconds: int = 0
    assigned_operator_id: int | None = None
    assigned_operator_name: str | None = None
    is_overdue: bool = False

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        return SlaIncidentStatus.coerce(value) or SlaIncidentStatus.PENDING.value

    @classmethod
    def from_incident(cls, incident: Any) -> Self:
        """Build the ``SLA_UPDATE`` payload from an ``SLAIncident`` row."""
        return cls(
            chat_id=incident.chat_id,
            incident_id=incident.id,
            status=incident.status,
            deadline_at=incident.deadline_at,
            remaining_seconds=incident.remaining_seconds,
            assigned_operator_id=incident.assigned_operator_id,
            assigned_operator_name=incident.assigned_operator_name,
            is_overdue=incident.is_overdue,
        )


class ChatLoadState(_ORMModel):
    """``SYSTEM``-authored operator load state, rendered next to the SLA badge."""

    level: Literal["free", "busy", "overloaded"] = "free"
    open_count: int = 0
    online_operators: int = 0
    avg_response_seconds: int = 0

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        if value not in ("free", "busy", "overloaded"):
            raise ValueError("level must be one of: free, busy, overloaded")
        return value


# ---------------------------------------------------------------------------
# Chats (CONTRACTS.md §3.3)
# ---------------------------------------------------------------------------


class ChatListLastMessage(_ORMModel):
    """The trimmed ``last_message`` object embedded in :class:`ChatListItem`."""

    id: int
    text: str = ""
    sender_name: str | None = None
    is_outgoing: bool = False
    created_at: UtcDatetime | None = None

    @classmethod
    def from_orm_message(cls, message: Any) -> Self:
        """Build from a ``GroupMessage`` row, applying the sender-name fallback."""
        return cls(
            id=message.id,
            text=message.text or "",
            sender_name=_resolve_sender_name(message),
            is_outgoing=bool(getattr(message, "is_outgoing", False)),
            created_at=message.created_at,
        )


class ChatListItem(_ORMModel):
    """One row of ``GET /api/chats`` and the ``data.chat`` payload of WS events."""

    id: int
    tg_chat_id: int
    title: str
    username: str | None = None
    merchant_code: str | None = None
    unread_count: int = 0
    assigned_operator_id: int | None = None
    assigned_operator_name: str | None = None
    last_message: ChatListLastMessage | None = None
    sla: SlaSnapshot | None = None
    #: Additive (documented) fields — never required by the frozen contract.
    last_message_at: UtcDatetime | None = None
    active_sla_count: int = 0
    load: ChatLoadState | None = None

    @field_validator("unread_count", "active_sla_count")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        return max(int(value), 0)

    @classmethod
    def build(
        cls,
        chat: Any,
        *,
        last_message: Any | None = None,
        sla: Any | None = None,
        active_sla_count: int | None = None,
        load: ChatLoadState | None = None,
    ) -> Self:
        """Convenience builder for services (``chat`` plus pre-fetched children).

        Services remain free to assemble the model field by field; this helper
        only centralises the assignment/display-name fallbacks.
        """
        operator = getattr(chat, "assigned_operator", None)
        if operator is not None:
            assigned_name = operator.display_name
        elif chat.assigned_operator_id is not None:
            assigned_name = f"Operator #{chat.assigned_operator_id}"
        else:
            assigned_name = None
        return cls(
            id=chat.id,
            tg_chat_id=chat.tg_chat_id,
            title=chat.title,
            username=chat.username,
            merchant_code=chat.merchant_code,
            unread_count=chat.unread_count or 0,
            assigned_operator_id=chat.assigned_operator_id,
            assigned_operator_name=assigned_name,
            last_message=(
                ChatListLastMessage.from_orm_message(last_message)
                if last_message is not None
                else None
            ),
            sla=SlaSnapshot.from_incident(sla) if sla is not None else None,
            last_message_at=getattr(chat, "last_message_at", None),
            active_sla_count=(
                active_sla_count
                if active_sla_count is not None
                else (1 if sla is not None else 0)
            ),
            load=load,
        )


class ChatListOut(_ORMModel):
    """``GET /api/chats`` response envelope."""

    items: list[ChatListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Messages (CONTRACTS.md §3.3 / §3.4)
# ---------------------------------------------------------------------------


def _resolve_sender_name(message: Any) -> str | None:
    """Resolve a display name for ``MessageOut.sender_name``.

    Operator messages fall back to ``sender_operator.display_name`` (then to a
    stable ``Operator #id`` label); inbound messages fall back to their stored
    ``sender_name`` and finally to the sender type label.
    """
    sender_type = getattr(message, "sender_type", None)
    own_name = getattr(message, "sender_name", None)
    if sender_type == MessageSenderType.OPERATOR.value:
        operator = getattr(message, "sender_operator", None)
        if operator is not None and operator.display_name:
            return operator.display_name
        if own_name:
            return own_name
        sender_operator_id = getattr(message, "sender_operator_id", None)
        if sender_operator_id is not None:
            return f"Operator #{sender_operator_id}"
        return "Operator"
    return own_name


def _safe_attr(message: Any, name: str) -> Any:
    """Read an optional attribute without risking ``MissingGreenlet``.

    A ``GroupMessage`` that was committed and refreshed has lazily-loaded columns, and
    touching an unloaded one from a synchronous serialiser raises. ``None`` is the
    correct answer for every optional field this is used with.
    """
    try:
        return getattr(message, name, None)
    except Exception:  # noqa: BLE001 - unloaded attribute must not break a page
        return None


def _sender_username(message: Any) -> str | None:
    """Telegram ``@username`` of the sender, when the row recorded one.

    The CRM stores it on ``GroupMessage.sender_username`` for inbound traffic only;
    operator messages have no Telegram username of their own. Accessing an attribute
    that was never loaded on a detached instance would raise ``MissingGreenlet`` in
    async SQLAlchemy, so the access is guarded and degrades to ``None``.
    """
    try:
        raw = getattr(message, "sender_username", None)
    except Exception:  # noqa: BLE001 - unloaded/detached attribute must not break a page
        return None
    if isinstance(raw, str):
        cleaned = raw.strip().lstrip("@")
        return cleaned or None
    return None


class MessageOut(_ORMModel):
    """``MessageOut`` — one chat message (CONTRACTS.md §3.3, CONTRACTS_V2 §3.1)."""

    id: int
    chat_id: int
    tg_msg_id: int | None = None
    sender_type: str = MessageSenderType.CLIENT.value
    sender_operator_id: int | None = None
    sender_name: str | None = None
    sender_tg_id: int | None = None
    sender_username: str | None = None
    text: str = ""
    reply_to_id: int | None = None
    reply_to_tg_msg_id: int | None = None
    phone: str | None = None
    is_deleted_in_tg: bool = False
    deleted_at: UtcDatetime | None = None
    #: CONTRACTS_V5 §1.2 — who deleted the message: ``'CRM'`` | ``'TELEGRAM'`` | ``None``.
    #: The flag alone cannot tell the two apart, so the timeline needs this to render an
    #: honest grey trace ("удалено из CRM" vs "удалено в Telegram").
    deleted_source: str | None = None
    sla_incident_id: int | None = None
    delivery_status: str = DeliveryStatus.SENT.value
    is_outgoing: bool = False
    created_at: UtcDatetime | None = None
    #: Additive (documented): lets the frontend render the mock TX status chip for
    #: seeded ``BOT`` alerts without a second request.
    tx_status: str | None = None
    updated_at: UtcDatetime | None = None
    #: CONTRACTS_V2 §3.1 — media payload attached to this message.
    kind: str = MessageKind.TEXT.value
    caption: str | None = None
    forwarded_from: str | None = None
    #: CONTRACTS_V3 §2.6 — when the text was last changed, and by whom. ``updated_at``
    #: also moves on deletion/reaction bookkeeping, so the UI needs the narrower signal
    #: to render the "(изменено)" badge honestly.
    edited_at: UtcDatetime | None = None
    edit_count: int = 0
    attachments: list["AttachmentOut"] = Field(default_factory=list)
    reactions: list["ReactionSummaryOut"] = Field(default_factory=list)

    @field_validator("sender_type")
    @classmethod
    def _validate_sender_type(cls, value: str) -> str:
        return MessageSenderType.coerce(value) or MessageSenderType.CLIENT.value

    @field_validator("delivery_status")
    @classmethod
    def _validate_delivery_status(cls, value: str) -> str:
        return DeliveryStatus.coerce(value) or DeliveryStatus.SENT.value

    @field_validator("tx_status")
    @classmethod
    def _validate_tx_status(cls, value: str | None) -> str | None:
        return TxStatus.coerce(value) if value else None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return MessageKind.coerce(value) or MessageKind.TEXT.value

    @model_validator(mode="after")
    def _derive_outgoing(self) -> Self:
        """Keep ``is_outgoing`` consistent with ``sender_type``."""
        expected = self.sender_type == MessageSenderType.OPERATOR.value
        if "is_outgoing" not in self.model_fields_set:
            object.__setattr__(self, "is_outgoing", expected)
        elif self.is_outgoing != expected:
            object.__setattr__(self, "is_outgoing", expected)
        return self

    @classmethod
    def from_orm_message(cls, message: Any) -> Self:
        """Build from a ``GroupMessage`` ORM instance.

        Maps the self-referential ``reply_to`` FK aliases (``reply_to_id`` /
        ``reply_to_tg_msg_id``) and resolves the operator display name.
        """
        # ``reply_to`` is a *scalar* FK reference (``uselist=False``), so unlike the
        # ``selectin``-loaded ``attachments``/``reactions`` collections it is genuinely
        # lazy: touching it from this synchronous serialiser emits SQL and raises
        # ``MissingGreenlet`` on every message that quotes another one. It is only a
        # fallback for ``reply_to_tg_msg_id``, so read it defensively.
        reply_to = _safe_attr(message, "reply_to")
        reply_to_id = getattr(message, "reply_to_id", None)
        reply_to_tg_msg_id = getattr(message, "reply_to_tg_msg_id", None)
        if reply_to_tg_msg_id is None and reply_to is not None:
            reply_to_tg_msg_id = getattr(reply_to, "tg_msg_id", None)
        sender_operator_id = getattr(message, "sender_operator_id", None)
        sender_operator = getattr(message, "sender_operator", None)
        if sender_operator_id is None and sender_operator is not None:
            sender_operator_id = sender_operator.id
        return cls(
            id=message.id,
            chat_id=message.chat_id,
            tg_msg_id=getattr(message, "tg_msg_id", None),
            sender_type=getattr(message, "sender_type", MessageSenderType.CLIENT.value),
            sender_operator_id=sender_operator_id,
            sender_name=_resolve_sender_name(message),
            sender_tg_id=getattr(message, "sender_tg_id", None),
            text=getattr(message, "text", "") or "",
            reply_to_id=reply_to_id,
            reply_to_tg_msg_id=reply_to_tg_msg_id,
            phone=getattr(message, "phone", None),
            is_deleted_in_tg=bool(getattr(message, "is_deleted_in_tg", False)),
            deleted_at=getattr(message, "deleted_at", None),
            # CONTRACTS_V5 §1.2: guarded like every other column here, so a build without
            # the column (or a detached instance) degrades to ``None`` instead of raising.
            deleted_source=_safe_attr(message, "deleted_source"),
            sla_incident_id=getattr(message, "sla_incident_id", None),
            delivery_status=getattr(message, "delivery_status", DeliveryStatus.SENT.value),
            is_outgoing=bool(getattr(message, "is_outgoing", False)),
            created_at=getattr(message, "created_at", None),
            tx_status=getattr(message, "tx_status", None),
            updated_at=getattr(message, "updated_at", None),
            # CONTRACTS_V2 §2.4: media flavour, caption and provenance. The column is
            # nullable for legacy rows, where TEXT is the correct default. Each access is
            # guarded because a detached instance can raise ``MissingGreenlet``.
            kind=_safe_attr(message, "kind") or MessageKind.TEXT.value,
            caption=_safe_attr(message, "caption"),
            forwarded_from=_safe_attr(message, "forwarded_from"),
            sender_username=_sender_username(message),
            # CONTRACTS_V3 §2.6: editing metadata. ``edits`` is selectin-loaded, so
            # summing it here costs no extra query; a Telegram edit is the only source
            # that records history, which is exactly what the badge should reflect.
            edited_at=_last_edit_time(message),
            edit_count=_edit_count(message),
            attachments=_attachments_out(message),
        )


def _attachments_out(message: Any) -> list[Any]:
    """Build ``AttachmentOut`` rows from the loaded ``attachments`` collection.

    The collection is ``lazy="selectin"``, so it is already populated for any row
    fetched through the ORM and this costs no extra query. Without it, every
    single-message response (``POST /forward``, ``PATCH``, ``POST /messages``)
    would claim a photo message carries no media — only the list endpoint used to
    fill these in.
    """
    rows = _safe_attr(message, "attachments")
    if not isinstance(rows, list):
        return []
    built: list[Any] = []
    for row in rows:
        try:
            built.append(AttachmentOut.from_orm_attachment(row))
        except Exception:  # noqa: BLE001 - one odd row must not break the message
            continue
    return built


def _edits_of(message: Any) -> list[Any]:
    """Return the loaded ``GroupMessage.edits`` collection, or an empty list."""
    try:
        edits = getattr(message, "edits", None)
    except Exception:  # noqa: BLE001 - unloaded collection on a detached instance
        return []
    return edits if isinstance(edits, list) else []


def _last_edit_time(message: Any) -> Any:
    """Timestamp of the newest recorded edit, if the message has any."""
    edits = _edits_of(message)
    if not edits:
        return None
    newest = max(edits, key=lambda e: (getattr(e, "created_at", None) is None, getattr(e, "created_at", None)))
    return getattr(newest, "created_at", None)


def _edit_count(message: Any) -> int:
    """How many times the text was changed from Telegram (CONTRACTS_V3 §2.1)."""
    return sum(1 for e in _edits_of(message) if getattr(e, "source", None) == "TELEGRAM")

    @classmethod
    def from_message(cls, message: Any) -> Self:
        """Alias of :meth:`from_orm_message` (either spelling is supported)."""
        return cls.from_orm_message(message)


class MessageListOut(_ORMModel):
    """``GET /api/chats/{chat_id}/messages`` response."""

    items: list[MessageOut] = Field(default_factory=list)
    has_more: bool = False


# ---------------------------------------------------------------------------
# Global search (CONTRACTS_V5 §6.2)
# ---------------------------------------------------------------------------


class SearchHitOut(_ORMModel):
    """One search result (CONTRACTS_V5 §6.2).

    ``snippet`` is cut **server-side** and ``match_spans`` holds ``[start, end)``
    offsets of the match *inside that snippet*, so the client highlights without
    re-implementing the matching rules. ``message`` is the full ``MessageOut`` of
    the hit, so a result renders as a normal message bubble.

    ``score`` is the PostgreSQL trigram ``similarity()`` of the needle against the
    message body (``0.0`` on a dialect without ``pg_trgm``, i.e. SQLite tests).
    """

    chat_id: int
    chat_title: str
    #: Merchant code of the chat; ``None`` when the chat carries none.
    chat_merchant_code: str | None = None
    message: MessageOut
    #: Window of the message text around the first match, ellipsis-trimmed.
    snippet: str = ""
    #: ``[[start, end], ...]`` offsets inside :attr:`snippet` (may be empty).
    match_spans: list[list[int]] = Field(default_factory=list)
    #: Relevance in ``[0.0, 1.0]``; ranking is ``score`` DESC, then ``created_at`` DESC.
    score: float = 0.0


class SearchResponseOut(_ORMModel):
    """``GET /api/search`` response (CONTRACTS_V5 §6.2)."""

    items: list[SearchHitOut] = Field(default_factory=list)
    #: Number of matches for the whole filter set, regardless of ``limit``/``offset``.
    total: int = 0
    #: ``True`` when another page exists after this one.
    has_more: bool = False
    #: Server-side duration of the request, in whole milliseconds.
    took_ms: int = 0


class SendMessageRequest(_StrictModel):
    """``POST /api/chats/{chat_id}/messages`` request body."""

    text: str = Field(min_length=1, max_length=4096)
    reply_to_msg_id: int | None = None
    client_nonce: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        if len(value) > 4096:
            raise ValueError("text must be at most 4096 characters")
        return value

    @field_validator("client_nonce")
    @classmethod
    def _validate_nonce(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("reply_to_msg_id")
    @classmethod
    def _validate_reply(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("reply_to_msg_id must be a positive integer")
        return value


class TypingRequest(_StrictModel):
    """``POST /api/chats/{chat_id}/typing`` request body."""

    action: Literal["start", "stop"]


class TypingResponse(_ORMModel):
    """``POST /api/chats/{chat_id}/typing`` response (CONTRACTS.md §3.5)."""

    ok: bool = True
    locked_by: str | None = None


# ---------------------------------------------------------------------------
# Transaction lookup (CONTRACTS.md §3.6)
# ---------------------------------------------------------------------------


class TxLookupRequest(_StrictModel):
    """``POST /api/tx/lookup`` request body."""

    tx_id: str = Field(min_length=2, max_length=64)
    panel: TxAdminPanel | None = None

    @field_validator("tx_id")
    @classmethod
    def _normalise_tx_id(cls, value: str) -> str:
        normalised = value.strip()
        if len(normalised) < 2:
            raise ValueError("tx_id must be at least 2 characters")
        return normalised.upper()

    @field_validator("panel", mode="before")
    @classmethod
    def _normalise_panel(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, TxAdminPanel):
            return value
        return TxAdminPanel.coerce(str(value))


class TxPanelResult(_ORMModel):
    """One admin panel's answer for a transaction."""

    panel: TxAdminPanel
    found: bool = False
    tx_id: str
    status: TxStatus = TxStatus.NOT_FOUND
    amount: str = Field(default="0.00", pattern=AMOUNT_PATTERN)
    currency: str = "USDT"
    provider: str | None = None
    error_reason: str | None = None
    external_ref: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount", mode="before")
    @classmethod
    def _normalise_amount(cls, value: Any) -> str:
        """Accept ``Decimal``/``int``/``float`` and always emit a 2dp string."""
        return format_decimal_2dp(value)

    @field_validator("panel", mode="before")
    @classmethod
    def _normalise_panel(cls, value: Any) -> Any:
        if isinstance(value, TxAdminPanel):
            return value
        return TxAdminPanel.coerce(str(value))


class TxLookupResponse(_ORMModel):
    """``POST /api/tx/lookup`` / ``GET /api/tx/lookup/{tx_id}`` response."""

    tx_id: str
    looked_up_at: UtcDatetime
    admin_one: TxPanelResult
    admin_two: TxPanelResult


class TxAuditItem(_ORMModel):
    """One ``tx_lookup_audit`` row (CONTRACTS.md §3.6)."""

    id: int
    operator_id: int
    operator_username: str | None = None
    tx_id: str
    admin_panel: str
    found: bool = False
    created_at: UtcDatetime | None = None
    #: Additive (documented): upstream status + latency for the audit drawer.
    response_status: str | None = None
    lookup_ms: int = 0

    @field_validator("admin_panel")
    @classmethod
    def _validate_admin_panel(cls, value: str) -> str:
        return TxAdminPanel.coerce(value) or TxAdminPanel.ADMIN_ONE.value

    @field_validator("response_status")
    @classmethod
    def _validate_response_status(cls, value: str | None) -> str | None:
        return TxStatus.coerce(value) if value else None

    @classmethod
    def from_audit(cls, audit: Any) -> Self:
        """Build from a ``TxLookupAudit`` ORM row (joins ``operator``)."""
        operator = getattr(audit, "operator", None)
        username = getattr(audit, "operator_username", None)
        if username is None and operator is not None:
            username = operator.username
        return cls(
            id=audit.id,
            operator_id=audit.operator_id,
            operator_username=username,
            tx_id=audit.tx_id,
            admin_panel=audit.admin_panel,
            found=bool(audit.found),
            created_at=getattr(audit, "created_at", None),
            response_status=getattr(audit, "response_status", None),
            lookup_ms=int(getattr(audit, "lookup_ms", 0) or 0),
        )


class TxAuditListOut(_ORMModel):
    """``GET /api/tx/audit`` response."""

    items: list[TxAuditItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Assignment / read markers / ops (CONTRACTS.md §3.3, §3.7)
# ---------------------------------------------------------------------------


class AssignRequest(_StrictModel):
    """``POST /api/chats/{chat_id}/assign`` request body."""

    operator_id: int | None = None

    @field_validator("operator_id")
    @classmethod
    def _validate_operator_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("operator_id must be a positive integer or null")
        return value


class ReadOut(_ORMModel):
    """``POST /api/chats/{chat_id}/read`` response."""

    ok: bool = True
    unread_count: int = 0

    @field_validator("unread_count")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        return max(int(value), 0)


class OkOut(_ORMModel):
    """Generic ``{"ok": true}`` response (broadcast, typing, locks)."""

    ok: bool = True


class SystemInfoOut(_ORMModel):
    """``GET /api/system/info`` response (CONTRACTS.md §3.7)."""

    telegram_connected: bool = False
    ws_clients: int = 0
    workers: dict[str, Any] = Field(default_factory=dict)
    sla_ticking: bool = False


class BroadcastRequest(_StrictModel):
    """``POST /api/internal/broadcast`` request body (supervisor only)."""

    event: dict[str, Any] = Field(min_length=1)
    target: Literal["ALL"] = "ALL"


class BroadcastOut(_ORMModel):
    """``POST /api/internal/broadcast`` response."""

    ok: bool = True


# ---------------------------------------------------------------------------
# WebSocket envelope (CONTRACTS.md §4)
# ---------------------------------------------------------------------------


def dump_model(model: BaseModel) -> dict[str, Any]:
    """Serialise any Pydantic model to a JSON-ready ``dict``.

    ``mode="json"`` guarantees datetimes are already ``"...Z"`` strings and enums
    are plain values, so the result can be handed straight to ``json.dumps`` /
    ``websockets.send``.
    """
    return model.model_dump(mode="json", exclude_none=False)


class WsEnvelope(_ORMModel):
    """Server -> client envelope: ``{"type": ..., "ts": ..., "data": {...}}``."""

    type: str
    ts: UtcDatetime
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls, event_type: str, data: dict[str, Any] | BaseModel | None = None
    ) -> Self:
        """Build an envelope stamped with ``utcnow()``."""
        if isinstance(data, BaseModel):
            payload = dump_model(data)
        else:
            payload = dict(data or {})
        return cls(type=event_type, ts=datetime.now(timezone.utc), data=payload)


def build_envelope(event_type: str, data: dict[str, Any] | BaseModel | None = None) -> dict[str, Any]:
    """Return a ready-to-send JSON dict for ``event_type`` (see :meth:`WsEnvelope.create`)."""
    return dump_model(WsEnvelope.create(event_type, data))


# ---- Server -> client payloads ---------------------------------------------


class WsConnectedData(_ORMModel):
    """``CONNECTED`` data payload."""

    operator: OperatorOut


class WsNewMessageData(_ORMModel):
    """``NEW_MESSAGE`` data payload: ``<MessageOut>`` + ``{"chat": <ChatListItem>}``."""

    message: MessageOut
    chat: ChatListItem

    @classmethod
    def from_orm_message(cls, message: Any, chat: ChatListItem) -> Self:
        """Build from a ``GroupMessage`` row plus a pre-built chat list item."""
        return cls(message=MessageOut.from_orm_message(message), chat=chat)

    def to_envelope_data(self) -> dict[str, Any]:
        """Flatten to the contract shape: message fields + ``data.chat``."""
        payload = self.message.model_dump(mode="json", exclude_none=False)
        payload["chat"] = self.chat.model_dump(mode="json", exclude_none=False)
        return payload


class WsMessageDeletedData(_ORMModel):
    """``MESSAGE_DELETED`` data payload."""

    message_id: int
    chat_id: int
    deleted_at: UtcDatetime | None = None
    message: MessageOut | None = None

    @classmethod
    def from_orm_message(cls, message: Any) -> Self:
        """Build from the (already soft-deleted) ``GroupMessage`` row."""
        return cls(
            message_id=message.id,
            chat_id=message.chat_id,
            deleted_at=getattr(message, "deleted_at", None),
            message=MessageOut.from_orm_message(message),
        )


class WsSlaUpdateData(ChatSlaOut):
    """``SLA_UPDATE`` data payload — identical shape to :class:`ChatSlaOut`."""


class WsSlaResolvedData(_ORMModel):
    """``SLA_RESOLVED`` data payload."""

    chat_id: int
    incident_id: int
    operator_id: int | None = None
    response_time_seconds: int | None = None
    stats: PersonalSlaStats | None = None

    @field_validator("response_time_seconds")
    @classmethod
    def _non_negative(cls, value: int | None) -> int | None:
        return None if value is None else max(int(value), 0)


class WsTypingLockData(_ORMModel):
    """``TYPING_LOCK`` data payload."""

    chat_id: int
    operator_id: int
    operator_name: str
    until: UtcDatetime | None = None


class WsTypingUnlockData(_ORMModel):
    """``TYPING_UNLOCK`` data payload."""

    chat_id: int
    operator_id: int | None = None


class WsShiftChangedData(OperatorOut):
    """``SHIFT_CHANGED`` data payload — the full ``OperatorOut`` object."""


class WsOperatorPresenceData(_ORMModel):
    """``OPERATOR_PRESENCE`` data payload."""

    operator_id: int
    is_online_ws: bool = False
    shift_status: str = ShiftStatus.OFFLINE.value

    @field_validator("shift_status")
    @classmethod
    def _validate_shift_status(cls, value: str) -> str:
        return ShiftStatus.coerce(value) or ShiftStatus.OFFLINE.value


class WsChatAssignedData(_ORMModel):
    """``CHAT_ASSIGNED`` data payload."""

    chat_id: int
    assigned_operator_id: int | None = None
    assigned_operator_name: str | None = None


class WsMessageStatusData(_ORMModel):
    """``MESSAGE_STATUS`` data payload (``tg_outgoing_ack`` fan-out)."""

    message_id: int
    chat_id: int
    delivery_status: str = DeliveryStatus.PENDING.value
    tg_msg_id: int | None = None

    @field_validator("delivery_status")
    @classmethod
    def _validate_delivery_status(cls, value: str) -> str:
        return DeliveryStatus.coerce(value) or DeliveryStatus.PENDING.value


class WsErrorData(_ORMModel):
    """``ERROR`` data payload."""

    detail: str


class WsPongData(_ORMModel):
    """``PONG`` data payload."""

    server_time: UtcDatetime


class WsBroadcastCommand(_StrictModel):
    """Normalised ``POST /api/internal/broadcast`` event after validation.

    ``event`` may be a full ``{"type": ..., "ts": ..., "data": {...}}`` envelope or
    a bare ``{"type": ..., "data": {...}}``; ``ts`` is filled in with ``utcnow()``
    when absent so the fan-out always emits a contract-compliant envelope.
    """

    type: str = Field(min_length=1, max_length=64)
    ts: UtcDatetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> Self:
        """Normalise a raw broadcast ``event`` object."""
        raw_type = event.get("type") or event.get("event") or ""
        if not isinstance(raw_type, str) or not raw_type.strip():
            raise ValueError("event.type is required")
        raw_data = event.get("data")
        if raw_data is None:
            raw_data = {}
        if not isinstance(raw_data, dict):
            raise ValueError("event.data must be an object")
        return cls(
            type=raw_type.strip().upper(),
            ts=event.get("ts"),
            data=raw_data,
        )

    def to_envelope(self) -> dict[str, Any]:
        """Return the final JSON envelope for the fan-out."""
        return build_envelope(self.type, self.data)


# ===========================================================================
# CONTRACTS_V2 — media attachments, reactions, admin
# ===========================================================================


class AttachmentOut(_ORMModel):
    """One media attachment of a message (CONTRACTS_V2 §3.1)."""

    id: int
    message_id: int
    kind: str = MessageKind.DOCUMENT.value
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None
    url: str = ""
    thumbnail_url: str | None = None

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        return MessageKind.coerce(value) or MessageKind.DOCUMENT.value

    @classmethod
    def from_orm_attachment(cls, attachment: Any) -> Self:
        """Build from a ``MediaAttachment`` row, deriving the public URLs."""
        attachment_id = int(getattr(attachment, "id"))
        has_thumb = bool(getattr(attachment, "thumbnail_path", None))
        return cls(
            id=attachment_id,
            message_id=int(getattr(attachment, "message_id")),
            kind=getattr(attachment, "kind", None) or MessageKind.DOCUMENT.value,
            file_name=getattr(attachment, "file_name", None),
            mime_type=getattr(attachment, "mime_type", None),
            size_bytes=getattr(attachment, "size_bytes", None),
            width=getattr(attachment, "width", None),
            height=getattr(attachment, "height", None),
            duration_seconds=getattr(attachment, "duration_seconds", None),
            url=f"/api/media/{attachment_id}",
            thumbnail_url=f"/api/media/{attachment_id}?variant=thumb" if has_thumb else None,
        )


class AttachmentListOut(_ORMModel):
    """``GET /api/messages/{id}/attachments``."""

    items: list[AttachmentOut] = Field(default_factory=list)


class ReactionRequest(_StrictModel):
    """``PUT /api/messages/{id}/reaction`` body."""

    emoji: str = Field(min_length=1, max_length=16)

    @field_validator("emoji")
    @classmethod
    def _validate_emoji(cls, value: str) -> str:
        normalised = value.strip()
        if normalised not in ReactionEmoji.values():
            allowed = ", ".join(ReactionEmoji.values())
            raise ValueError(f"unsupported reaction {value!r} (allowed: {allowed})")
        return normalised


class ReactionSummaryOut(_ORMModel):
    """One aggregated emoji on a message (CONTRACTS_V2 §3.2)."""

    emoji: str
    count: int = 0
    #: True when the calling operator is among the reactors.
    me: bool = False
    operators: list[str] = Field(default_factory=list)
    operator_ids: list[int] = Field(default_factory=list)


class ReactionListOut(_ORMModel):
    """``GET /api/messages/{id}/reactions``."""

    items: list[ReactionSummaryOut] = Field(default_factory=list)


class OperatorAdminOut(_ORMModel):
    """Admin view of an operator record (CONTRACTS_V2 §3.3)."""

    id: int
    username: str
    display_name: str = ""
    role: str = OperatorRole.OPERATOR.value
    shift_status: str = ShiftStatus.OFFLINE.value
    is_active: bool = True
    max_chats: int | None = None
    can_delete_messages: bool = False
    can_export: bool = False
    avatar_color: str | None = None
    #: CONTRACTS_V5 §2.3: the per-operator SLA override (highest priority). Always
    #: reported so the admin panel can show whether the account deviates from the
    #: global ``app_settings.sla_target_seconds``.
    sla_target_seconds: int = 300
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return OperatorRole.coerce(value) or OperatorRole.OPERATOR.value

    @field_validator("shift_status")
    @classmethod
    def _validate_shift(cls, value: str) -> str:
        return ShiftStatus.coerce(value) or ShiftStatus.OFFLINE.value


class OperatorAdminListOut(_ORMModel):
    """``GET /api/admin/operators``."""

    items: list[OperatorAdminOut] = Field(default_factory=list)


#: The SLA target bounds (``SLA_TARGET_SECONDS_MIN`` / ``SLA_TARGET_SECONDS_MAX`` /
#: ``DEFAULT_SLA_TARGET_SECONDS``) are declared next to :data:`UtcDatetime` at the top of
#: the module, because the operator request models below share them with the admin
#: settings block; importing them from there would create a cycle with ``app.models``.


class OperatorCreateRequest(_StrictModel):
    """``POST /api/admin/operators`` body."""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: str = OperatorRole.OPERATOR.value
    is_active: bool = True
    max_chats: int | None = Field(default=None, ge=0, le=500)
    can_delete_messages: bool = False
    can_export: bool = False
    avatar_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    #: CONTRACTS_V5 §2.3 per-operator SLA override; omitted -> the column default, so
    #: the global ``app_settings.sla_target_seconds`` governs the new account.
    sla_target_seconds: int | None = Field(
        default=None, ge=SLA_TARGET_SECONDS_MIN, le=SLA_TARGET_SECONDS_MAX
    )

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return OperatorRole.coerce(value) or OperatorRole.OPERATOR.value


class OperatorUpdateRequest(_StrictModel):
    """``PATCH /api/admin/operators/{id}`` body — every field optional."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: str | None = None
    is_active: bool | None = None
    max_chats: int | None = Field(default=None, ge=0, le=500)
    can_delete_messages: bool | None = None
    can_export: bool | None = None
    avatar_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    #: CONTRACTS_V5 §2.3 per-operator SLA override. ``None`` (omitted) leaves the
    #: current value untouched; the bounds mirror the ``app_settings`` CHECK so both
    #: levels of the priority chain accept the same numeric window.
    sla_target_seconds: int | None = Field(
        default=None, ge=SLA_TARGET_SECONDS_MIN, le=SLA_TARGET_SECONDS_MAX
    )

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str | None) -> str | None:
        return OperatorRole.coerce(value) if value else None


class PasswordResetRequest(_StrictModel):
    """``POST /api/admin/operators/{id}/reset-password`` body."""

    new_password: str = Field(min_length=8, max_length=128)


class TelegramAccountOut(_ORMModel):
    """Admin view of a Telegram account — secrets are always masked."""

    id: int
    label: str
    tg_user_id: int | None = None
    tg_username: str | None = None
    phone: str | None = None
    api_id: int | None = None
    is_active: bool = True
    has_session: bool = False
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class TelegramAccountListOut(_ORMModel):
    """``GET /api/admin/accounts``."""

    items: list[TelegramAccountOut] = Field(default_factory=list)


class TelegramAccountCreateRequest(_StrictModel):
    """``POST /api/admin/accounts`` body."""

    label: str = Field(min_length=1, max_length=64)
    tg_user_id: int | None = None
    tg_username: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    api_id: int | None = Field(default=None, ge=1)
    api_hash: str | None = Field(default=None, max_length=64)
    session_string: str | None = None
    is_active: bool = True


class TelegramAccountUpdateRequest(_StrictModel):
    """``PATCH /api/admin/accounts/{id}`` body — every field optional."""

    label: str | None = Field(default=None, min_length=1, max_length=64)
    tg_user_id: int | None = None
    tg_username: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    api_id: int | None = Field(default=None, ge=1)
    api_hash: str | None = Field(default=None, max_length=64)
    session_string: str | None = None
    is_active: bool | None = None


class AuditEntryOut(_ORMModel):
    """One administrative action (``GET /api/admin/audit``)."""

    id: int
    actor_id: int | None = None
    actor_username: str | None = None
    action: str
    target_type: str | None = None
    target_id: int | None = None
    detail: str | None = None
    created_at: UtcDatetime | None = None


class AuditListOut(_ORMModel):
    """``GET /api/admin/audit``."""

    items: list[AuditEntryOut] = Field(default_factory=list)


class AdminLoginRequest(_StrictModel):
    """``POST /api/admin/login`` body."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


# ===========================================================================
# CONTRACTS_V5 §2 — SLA settings (admin panel)
# ===========================================================================

# The bounds above are declared near the top of the module (they are used by
# ``OperatorCreateRequest``/``OperatorUpdateRequest``), so they are not repeated here.
# See "CONTRACTS_V5 §2 — SLA bounds" before the operator request models.


class AdminSettingsOut(_ORMModel):
    """``GET``/``PATCH /api/admin/settings`` response (CONTRACTS_V5 §2.2).

    Exactly the five keys of the frozen contract::

        { "sla_enabled": true, "sla_target_seconds": 1800,
          "sla_reset_on_operator_reply": true, "updated_at": "...", "updated_by": "admin" }

    ``updated_at`` / ``updated_by`` are ``None`` only for a row that was never written
    by an administrator (the bootstrap row), so the panel can render
    «значения по умолчанию».
    """

    sla_enabled: bool = True
    sla_target_seconds: int = Field(
        default=DEFAULT_SLA_TARGET_SECONDS,
        ge=SLA_TARGET_SECONDS_MIN,
        le=SLA_TARGET_SECONDS_MAX,
        description="Глобальный целевой SLA в секундах (60..86400).",
    )
    sla_reset_on_operator_reply: bool = True
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None

    @classmethod
    def from_settings(cls, row: Any) -> Self:
        """Build from an ``AppSettings`` row or the service's read-only snapshot.

        Attribute access goes through ``getattr`` with the documented defaults so the
        same builder serves the ORM row (fresh read) and the cached snapshot, and a
        missing column can never turn a settings read into a ``500``.
        """
        return cls(
            sla_enabled=bool(getattr(row, "sla_enabled", True)),
            sla_target_seconds=getattr(row, "sla_target_seconds", DEFAULT_SLA_TARGET_SECONDS),
            sla_reset_on_operator_reply=bool(getattr(row, "sla_reset_on_operator_reply", True)),
            updated_at=getattr(row, "updated_at", None),
            updated_by=getattr(row, "updated_by", None),
        )


class AdminSettingsUpdate(_StrictModel):
    """``PATCH /api/admin/settings`` body (CONTRACTS_V5 §2.2) — every field optional.

    Only the fields the client actually sent are applied
    (``model_dump(exclude_unset=True)`` in the service), so
    ``PATCH {"sla_enabled": false}`` can never silently reset the target.

    ``extra="forbid"`` (inherited from :class:`_StrictModel`) turns a typo such as
    ``sla_target`` into a ``422`` instead of a silently ignored no-op: a settings write
    is exactly the kind of request a client retries verbatim and never re-inspects.
    ``sla_target_seconds`` is validated against the same window as the database
    ``CHECK`` (``60..86400``, §2.1), so a bad value fails with a readable ``422``
    before the flush.
    """

    sla_enabled: bool | None = Field(
        default=None, description="Включить/выключить создание новых SLA-инцидентов."
    )
    sla_target_seconds: int | None = Field(
        default=None,
        ge=SLA_TARGET_SECONDS_MIN,
        le=SLA_TARGET_SECONDS_MAX,
        description="Глобальный целевой SLA в секундах (60..86400).",
    )
    sla_reset_on_operator_reply: bool | None = Field(
        default=None,
        description="Ответ оператора без цитаты закрывает открытый инцидент (правило V5 §3).",
    )


# ===========================================================================
# CONTRACTS_V3 — message editing and forwarding
# ===========================================================================

#: Hard cap enforced by Telegram on a message body (mirrors ``messages.MAX_TEXT_LENGTH``).
MAX_MESSAGE_TEXT_LENGTH: Final[int] = 4096
#: Telegram's own limit for the comment stacked above a forwarded body (§1.2).
MAX_FORWARD_COMMENT_LENGTH: Final[int] = 1024


class MessageEditRequest(_StrictModel):
    """``PATCH /api/messages/{message_id}`` body (CONTRACTS_V3 §2.3)."""

    text: str = Field(min_length=1, max_length=MAX_MESSAGE_TEXT_LENGTH)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        """Reject blank-after-strip bodies and texts above the Telegram cap."""
        if not value.strip():
            raise ValueError("text must not be blank")
        if len(value) > MAX_MESSAGE_TEXT_LENGTH:
            raise ValueError(f"text must be at most {MAX_MESSAGE_TEXT_LENGTH} characters")
        return value


class MessageEditOut(_ORMModel):
    """One recorded edit (CONTRACTS_V3 §2.2/§2.3).

    Only ``source='TELEGRAM'`` edits are persisted: a CRM-side edit is our own
    action and is never written to ``message_edits`` (invariant §5.3).
    """

    id: int
    message_id: int
    source: str = EditSource.TELEGRAM.value
    old_text: str = ""
    new_text: str = ""
    operator_id: int | None = None
    actor_name: str | None = None
    created_at: UtcDatetime | None = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        return EditSource.coerce(value) or EditSource.TELEGRAM.value

    @classmethod
    def from_orm_edit(cls, edit: Any) -> Self:
        """Build from a ``MessageEdit`` row (attributes only, never lazy IO)."""
        return cls(
            id=int(getattr(edit, "id")),
            message_id=int(getattr(edit, "message_id")),
            source=getattr(edit, "source", EditSource.TELEGRAM.value),
            old_text=getattr(edit, "old_text", "") or "",
            new_text=getattr(edit, "new_text", "") or "",
            operator_id=getattr(edit, "operator_id", None),
            actor_name=getattr(edit, "actor_name", None),
            created_at=getattr(edit, "created_at", None),
        )


class MessageEditListOut(_ORMModel):
    """``GET /api/messages/{message_id}/edits`` response (CONTRACTS_V3 §2.3)."""

    items: list[MessageEditOut] = Field(default_factory=list)


class MessageUpdateOut(_ORMModel):
    """``PATCH /api/messages/{message_id}`` response (CONTRACTS_V3 §2.3).

    ``edited_in_telegram`` is ``True`` when a ``tg_outgoing`` command carrying
    ``edit_tg_msg_id`` was published — i.e. the edit will reach Telegram too. Mock
    chats (``tg_chat_id IS NULL``) never report ``True``.
    """

    message: MessageOut
    edited_in_telegram: bool = False


class ForwardRequest(_StrictModel):
    """``POST /api/messages/{message_id}/forward`` body (CONTRACTS_V3 §1.2)."""

    target_chat_id: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=MAX_FORWARD_COMMENT_LENGTH)

    @field_validator("comment")
    @classmethod
    def _normalise_comment(cls, value: str | None) -> str | None:
        """Trim the comment and treat a blank one as absent."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_FORWARD_COMMENT_LENGTH:
            raise ValueError(
                f"comment must be at most {MAX_FORWARD_COMMENT_LENGTH} characters"
            )
        return cleaned


# ---------------------------------------------------------------------------
# CONTRACTS_V5 §1.3 — deleting a message from the CRM (outbound deletion)
# ---------------------------------------------------------------------------


class TelegramDeliveryInfo(_ORMModel):
    """``telegram`` block of :class:`DeletedMessageOut` (CONTRACTS_V5 §1.3).

    * ``requested`` — a ``tg_delete_out`` command was published (the message had both
      ``tg_msg_id`` and its chat had ``tg_chat_id``);
    * ``delivered`` — the command reached Redis. ``False`` does **not** make the
      request an error: the CRM row is already committed (§1.2 / §8 invariant 3), a
      missed Telegram deletion is only reported here;
    * ``error`` — why delivery failed, or ``None``.
    """

    requested: bool = False
    delivered: bool = False
    error: str | None = None


class DeletedMessageOut(_ORMModel):
    """``DELETE /api/messages/{message_id}`` response (CONTRACTS_V5 §1.3).

    ``message`` is the full :class:`MessageOut` of the now soft-deleted row
    (``is_deleted_in_tg=True``, ``deleted_at`` set), so the frontend can repaint the
    bubble as a grey trace without re-fetching the timeline. The row is never removed
    from ``group_messages`` (§1.2).
    """

    message_id: int
    chat_id: int
    deleted_at: UtcDatetime
    deleted_source: str
    message: MessageOut
    telegram: TelegramDeliveryInfo = Field(default_factory=TelegramDeliveryInfo)


# ---------------------------------------------------------------------------
# CONTRACTS_V5 §5.2 — editable quick replies
# ---------------------------------------------------------------------------

#: ``quick_replies.shortcut`` limits (§5.1 ``VARCHAR(32)`` / §5.2 validation).
QUICK_REPLY_SHORTCUT_MAX_LENGTH: Final[int] = 32
#: ``quick_replies.title`` limit (§5.1 ``VARCHAR(120)``).
QUICK_REPLY_TITLE_MAX_LENGTH: Final[int] = 120
#: ``quick_replies.category`` limit (§5.1 ``VARCHAR(40)``); the default is ``general``.
QUICK_REPLY_CATEGORY_MAX_LENGTH: Final[int] = 40
#: Fallback category when a reply does not pick one (§5.1 ``DEFAULT 'general'``).
QUICK_REPLY_DEFAULT_CATEGORY: Final[str] = "general"
#: Hard cap on a reply body. ``body`` is ``TEXT`` (unbounded) in the database, but a
#: canned response that does not fit a Telegram message could never be sent, so the
#: API refuses it up front instead of letting the composer fail later.
QUICK_REPLY_BODY_MAX_LENGTH: Final[int] = MAX_MESSAGE_TEXT_LENGTH
#: Allowed ``shortcut`` characters: letters, digits, ``-`` and ``_`` (§5.2).
#:
#: The shortcut is typed after a slash in the composer and echoed into logs and
#: audit trails, so anything that needs quoting or escaping — spaces, slashes,
#: ``%``, quotes, non-ASCII look-alikes — is rejected at the edge rather than
#: sanitised silently. ``re.ASCII`` semantics come from the explicit character
#: class: ``"ıx"`` or ``"аx"`` (Cyrillic «а») would otherwise pass a looser
#: ``isalpha()`` check and produce two shortcuts that look identical in the UI
#: while being distinct keys in the unique index.
QUICK_REPLY_SHORTCUT_PATTERN: Final[str] = r"^[A-Za-z0-9_-]+$"


def normalise_quick_reply_shortcut(value: Any) -> str:
    """Return the canonical ``shortcut`` or raise :class:`ValueError`.

    Trims surrounding whitespace, lower-cases (shortcuts are case-insensitive in
    the UI, so ``"TX"`` and ``"tx"`` must not become two rows) and enforces
    1..:data:`QUICK_REPLY_SHORTCUT_MAX_LENGTH` characters of
    :data:`QUICK_REPLY_SHORTCUT_PATTERN`.
    """
    candidate = "" if value is None else str(value).strip().lower()
    if not candidate:
        raise ValueError("shortcut must not be blank")
    if len(candidate) > QUICK_REPLY_SHORTCUT_MAX_LENGTH:
        raise ValueError(
            f"shortcut must be at most {QUICK_REPLY_SHORTCUT_MAX_LENGTH} characters"
        )
    if re.fullmatch(QUICK_REPLY_SHORTCUT_PATTERN, candidate) is None:
        raise ValueError("shortcut may contain only letters, digits, '-' and '_'")
    return candidate


def normalise_quick_reply_title(value: Any) -> str:
    """Return the trimmed ``title`` (1..:data:`QUICK_REPLY_TITLE_MAX_LENGTH`)."""
    candidate = "" if value is None else str(value).strip()
    if not candidate:
        raise ValueError("title must not be blank")
    if len(candidate) > QUICK_REPLY_TITLE_MAX_LENGTH:
        raise ValueError(
            f"title must be at most {QUICK_REPLY_TITLE_MAX_LENGTH} characters"
        )
    return candidate


def normalise_quick_reply_body(value: Any) -> str:
    """Return the ``body`` verbatim, rejecting one blank after ``btrim``.

    The text is **not** trimmed: leading indentation and a trailing newline are
    meaningful inside a canned message, and §5.1 only forbids a *blank* body.
    """
    candidate = "" if value is None else str(value)
    if not candidate.strip():
        raise ValueError("body must not be blank")
    if len(candidate) > QUICK_REPLY_BODY_MAX_LENGTH:
        raise ValueError(
            f"body must be at most {QUICK_REPLY_BODY_MAX_LENGTH} characters"
        )
    return candidate


def normalise_quick_reply_category(value: Any) -> str:
    """Return the trimmed ``category``, falling back to ``general`` when omitted."""
    candidate = "" if value is None else str(value).strip()
    if not candidate:
        return QUICK_REPLY_DEFAULT_CATEGORY
    if len(candidate) > QUICK_REPLY_CATEGORY_MAX_LENGTH:
        raise ValueError(
            f"category must be at most {QUICK_REPLY_CATEGORY_MAX_LENGTH} characters"
        )
    return candidate


class QuickReplyIn(_StrictModel):
    """``POST /api/quick-replies`` body (CONTRACTS_V5 §5.2).

    ``shortcut``, ``title`` and ``body`` are required; ``category``,
    ``sort_order`` and ``is_active`` carry the §5.1 column defaults so a minimal
    client can create a usable reply from three fields.
    """

    shortcut: str = Field(min_length=1, max_length=QUICK_REPLY_SHORTCUT_MAX_LENGTH)
    title: str = Field(min_length=1, max_length=QUICK_REPLY_TITLE_MAX_LENGTH)
    body: str = Field(min_length=1, max_length=QUICK_REPLY_BODY_MAX_LENGTH)
    category: str = Field(
        default=QUICK_REPLY_DEFAULT_CATEGORY, max_length=QUICK_REPLY_CATEGORY_MAX_LENGTH
    )
    sort_order: int = 0
    is_active: bool = True

    _normalise_shortcut = field_validator("shortcut")(normalise_quick_reply_shortcut)
    _normalise_title = field_validator("title")(normalise_quick_reply_title)
    _normalise_body = field_validator("body")(normalise_quick_reply_body)
    _normalise_category = field_validator("category")(normalise_quick_reply_category)

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: int) -> int:
        """Keep ``sort_order`` inside a sane range so ordering stays meaningful."""
        if not -1_000_000 <= int(value) <= 1_000_000:
            raise ValueError("sort_order must be between -1000000 and 1000000")
        return int(value)


class QuickReplyUpdate(_StrictModel):
    """``PATCH /api/quick-replies/{id}`` body (CONTRACTS_V5 §5.2).

    Every field is optional; only those the client sends are applied
    (:meth:`changes`). An explicitly ``null`` non-nullable field —
    ``shortcut``/``title``/``body``, all ``NOT NULL`` in §5.1 — is rejected rather
    than silently ignored, so a client bug surfaces as a ``422``.
    """

    shortcut: str | None = Field(default=None, max_length=QUICK_REPLY_SHORTCUT_MAX_LENGTH)
    title: str | None = Field(default=None, max_length=QUICK_REPLY_TITLE_MAX_LENGTH)
    body: str | None = Field(default=None, max_length=QUICK_REPLY_BODY_MAX_LENGTH)
    category: str | None = Field(default=None, max_length=QUICK_REPLY_CATEGORY_MAX_LENGTH)
    sort_order: int | None = None
    is_active: bool | None = None

    @field_validator("shortcut")
    @classmethod
    def _validate_shortcut(cls, value: str | None) -> str | None:
        return None if value is None else normalise_quick_reply_shortcut(value)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        return None if value is None else normalise_quick_reply_title(value)

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str | None) -> str | None:
        return None if value is None else normalise_quick_reply_body(value)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str | None) -> str | None:
        return None if value is None else normalise_quick_reply_category(value)

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not -1_000_000 <= int(value) <= 1_000_000:
            raise ValueError("sort_order must be between -1000000 and 1000000")
        return int(value)

    @model_validator(mode="after")
    def _reject_empty_patch(self) -> Self:
        """An empty ``PATCH`` is a client bug: fail loudly instead of a no-op ``200``."""
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self

    def changes(self) -> dict[str, Any]:
        """Only the fields the client actually sent, with normalised values."""
        return self.model_dump(exclude_unset=True, exclude_none=True)


class QuickReplyOut(_ORMModel):
    """One quick reply (CONTRACTS_V5 §5.2).

    ``created_by`` is the authoring operator's **display name**, not the id: the
    editor renders it as a caption and the client has no way to resolve a bare
    operator id. It is ``None`` when the author's account was deleted (the FK is
    ``ON DELETE SET NULL``) or when the row was inserted by the seed.
    """

    id: int
    shortcut: str
    title: str
    body: str
    category: str = QUICK_REPLY_DEFAULT_CATEGORY
    sort_order: int = 0
    is_active: bool = True
    created_by: str | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None

    @classmethod
    def from_orm_reply(cls, reply: Any) -> Self:
        """Build from a ``QuickReply`` row, resolving the author's display name.

        Reads ``reply.created_by`` — the model declares that relationship with
        ``lazy="selectin"``, so resolving it costs no extra query for a list that
        was just fetched.
        """
        author = getattr(reply, "created_by", None)
        name: str | None = None
        if author is not None:
            name = (
                str(getattr(author, "display_name", "") or "").strip()
                or str(getattr(author, "username", "") or "").strip()
                or None
            )
        return cls(
            id=int(getattr(reply, "id")),
            shortcut=str(getattr(reply, "shortcut", "") or ""),
            title=str(getattr(reply, "title", "") or ""),
            body=str(getattr(reply, "body", "") or ""),
            category=str(
                getattr(reply, "category", "") or QUICK_REPLY_DEFAULT_CATEGORY
            ),
            sort_order=int(getattr(reply, "sort_order", 0) or 0),
            is_active=bool(getattr(reply, "is_active", True)),
            created_by=name,
            created_at=getattr(reply, "created_at", None),
            updated_at=getattr(reply, "updated_at", None),
        )


class QuickReplyListOut(_ORMModel):
    """``GET /api/quick-replies`` response (CONTRACTS_V5 §5.2).

    The identical shape is the ``data`` of the §5.3 ``QUICK_REPLIES_UPDATED`` WS
    event (``{"items": [...]}``), so one client handler renders both.
    """

    items: list[QuickReplyOut] = Field(default_factory=list)

    @classmethod
    def from_orm_replies(cls, replies: Any) -> Self:
        """Build from an iterable of ``QuickReply`` rows, preserving their order."""
        return cls(items=[QuickReplyOut.from_orm_reply(reply) for reply in replies or ()])

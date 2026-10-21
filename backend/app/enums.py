"""Canonical string enums from CONTRACTS.md §2.

This module is a **pure leaf**: it imports nothing from the application, so every
other module (models, schemas, security, redis_bus, services, routers, seed) may
import it without any risk of a cycle.

All members are ``str`` subclasses with ``__str__`` returning ``.value`` so that
``f"{ShiftStatus.ONLINE}" == "ONLINE"``, JSON serialisation yields the bare
string, and values can be written straight into ``VARCHAR`` columns.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "DeliveryStatus",
    "EditSource",
    "MessageKind",
    "MessageSenderType",
    "OperatorRole",
    "ReactionEmoji",
    "SHIFT_DEFAULTS",
    "ShiftStatus",
    "SlaIncidentStatus",
    "TxAdminPanel",
    "TxStatus",
]


class _StrEnum(str, Enum):
    """``str``-backed enum whose ``str()`` is the raw database value."""

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, format_spec: str) -> str:
        return format(str(self.value), format_spec)

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Return every member value as a tuple (handy for CHECK constraints)."""
        return tuple(member.value for member in cls)

    @classmethod
    def coerce(cls, raw: str | None) -> str | None:
        """Normalise arbitrary user input to a canonical member value.

        Accepts mixed case and surrounding whitespace. Returns ``None`` for
        ``None``/empty input and raises ``ValueError`` for unknown values.
        """
        if raw is None:
            return None
        candidate = str(raw).strip()
        if not candidate:
            return None
        try:
            return cls(candidate).value
        except ValueError:
            pass
        try:
            return cls(candidate.upper()).value
        except ValueError:
            member = getattr(cls, candidate.upper(), None)
            if member is not None:
                return member.value
        valid = ", ".join(member.value for member in cls)
        raise ValueError(f"invalid {cls.__name__}: {raw!r} (valid: {valid})")


class ShiftStatus(_StrEnum):
    """Operator shift state. ``ONLINE`` -> ``PAUSE`` (never straight to OFFLINE)."""

    ONLINE = "ONLINE"
    PAUSE = "PAUSE"
    OFFLINE = "OFFLINE"


class MessageSenderType(_StrEnum):
    """Who authored a chat message."""

    OPERATOR = "OPERATOR"
    CLIENT = "CLIENT"
    BOT = "BOT"
    SYSTEM = "SYSTEM"


class DeliveryStatus(_StrEnum):
    """Outbound delivery state of an operator message (``tg_outgoing_ack``)."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class SlaIncidentStatus(_StrEnum):
    """Lifecycle of an SLA incident."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"


class TxAdminPanel(_StrEnum):
    """Which upstream admin panel a transaction lookup targets."""

    ADMIN_ONE = "ADMIN_ONE"
    ADMIN_TWO = "ADMIN_TWO"


class TxStatus(_StrEnum):
    """Transaction status as reported by the (mock) admin panels."""

    COMPLETED = "COMPLETED"
    PROCESSING = "PROCESSING"
    PENDING = "PENDING"
    FAILED = "FAILED"
    DECLINED = "DECLINED"
    REFUNDED = "REFUNDED"
    NOT_FOUND = "NOT_FOUND"


class OperatorRole(_StrEnum):
    """``operators.role`` values (CONTRACTS.md §3.2, CONTRACTS_V2.md §1)."""

    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"
    #: Full access: operator management, Telegram accounts, audit log (CONTRACTS_V2.md §3.3).
    ADMIN = "ADMIN"


class MessageKind(_StrEnum):
    """Media kind of a ``group_messages`` row (CONTRACTS_V2.md §1/§2.4).

    ``TEXT`` is the default for every message without attachments; the remaining
    members mirror what :class:`~app.models.MediaAttachment` may carry.
    """

    TEXT = "TEXT"
    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    VOICE = "VOICE"
    VIDEO = "VIDEO"
    STICKER = "STICKER"
    AUDIO = "AUDIO"
    OTHER = "OTHER"


class ReactionEmoji(_StrEnum):
    """The six reaction emoji the CRM offers (CONTRACTS_V2.md §1) — exactly six.

    Members are multi-character strings (``❤️`` is U+2764 U+FE0F), so the
    ``CHECK`` constraint built from :meth:`_StrEnum.values` is exact and the raw
    value is what both the API and Telegram send back.
    """

    THUMBS_UP = "👍"
    HEART = "❤️"
    FIRE = "🔥"
    EYES = "👀"
    CLAP = "👏"
    PRAY = "🙏"


class EditSource(_StrEnum):
    """Кто изменил текст сообщения (CONTRACTS_V3 §2).

    ``TELEGRAM`` — правка пришла из Telegram (``tg_edits``): текст в CRM
    обновляется, **а старая версия сохраняется** в ``message_edits``.
    ``CRM`` — правку сделал оператор: текст уходит в Telegram, но история
    правок **не ведётся** (§2.1, инвариант §5.3).
    """

    TELEGRAM = "TELEGRAM"
    CRM = "CRM"


#: When the WS heartbeat times out, a shift falls back ONLINE -> PAUSE (never to
#: OFFLINE) per CONTRACTS.md §4.
SHIFT_DEFAULTS: dict[str, str] = {
    ShiftStatus.ONLINE.value: ShiftStatus.PAUSE.value,
    ShiftStatus.PAUSE.value: ShiftStatus.PAUSE.value,
    ShiftStatus.OFFLINE.value: ShiftStatus.OFFLINE.value,
}

"""Additive response envelopes for shapes CONTRACTS.md describes as anonymous objects.

``CONTRACTS.md`` freezes several payloads as bare JSON objects rather than named
pydantic models (``{"items": [...]}`` lists, the §3.3 ``sla`` snapshot). FastAPI
needs a concrete ``response_model`` to document and validate them, so those
envelopes live here instead of in ``app.schemas``:

* nothing here renames or reshapes a contract field — every nested model is the
  frozen one from ``app.schemas``;
* keeping them separate means ``app/schemas.py`` (owned by another agent) is
  never modified by the routers.

Field sets mirror the contract exactly — ``SlaSnapshotOut`` is the §3.3 ``sla``
object (``status``, ``deadline_at``, ``remaining_seconds``, ``is_overdue``); the
richer identifier-bearing variant used by ``SLA_UPDATE`` is
``app.schemas.ChatSlaOut``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.enums import SlaIncidentStatus
from app.schemas import ChatListItem, OperatorOut, TxAuditItem, UtcDatetime

__all__ = [
    "ChatListResponse",
    "OperatorListResponse",
    "SlaIncidentListResponse",
    "SlaSnapshotOut",
    "TxAuditListResponse",
]


class SlaSnapshotOut(BaseModel):
    """§3.3 ``ChatListItem.sla`` object (no identifiers — see ``ChatSlaOut``)."""

    model_config = ConfigDict(extra="ignore")

    status: SlaIncidentStatus
    deadline_at: UtcDatetime
    remaining_seconds: int
    is_overdue: bool


class ChatListResponse(BaseModel):
    """``GET /api/chats`` -> ``{"items": [ChatListItem, ...]}``."""

    model_config = ConfigDict(extra="ignore")

    items: list[ChatListItem] = Field(default_factory=list)


class OperatorListResponse(BaseModel):
    """``GET /api/shift/operators`` -> ``{"items": [OperatorOut, ...]}``."""

    model_config = ConfigDict(extra="ignore")

    items: list[OperatorOut] = Field(default_factory=list)


class SlaIncidentListResponse(BaseModel):
    """``GET /api/sla/incidents`` -> ``{"items": [SlaSnapshot, ...]}``.

    ``SlaSnapshot`` is the §3.3 four-field object; ``SlaSnapshotOut`` is declared
    locally so the route does not depend on which variant ``app.schemas``
    exposes as ``SlaSnapshot``.
    """

    model_config = ConfigDict(extra="ignore")

    items: list[SlaSnapshotOut] = Field(default_factory=list)


class TxAuditListResponse(BaseModel):
    """``GET /api/tx/audit`` -> ``{"items": [TxAuditItem, ...]}``."""

    model_config = ConfigDict(extra="ignore")

    items: list[TxAuditItem] = Field(default_factory=list)

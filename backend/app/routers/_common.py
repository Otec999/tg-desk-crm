"""Shared helpers for the REST routers (serialisation + broadcast plumbing).

Routers never hand-build API payloads from raw ORM rows: they go through this
module, which delegates to the canonical ``app.schemas`` builders
(``OperatorOut.from_operator``, ``MessageOut.from_orm_message``,
``ChatListItem.build``, ``SlaSnapshot.from_incident``) and keeps a field-exact
fallback for the shapes that have no classmethod, so every response matches
``CONTRACTS.md`` §3/§4 even if a helper signature drifts.

Broadcasting builds envelopes through ``ws_manager.envelope`` and swallows every
transport error: a dead Redis/WebSocket layer must never fail an HTTP request.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from app.models import DeliveryStatus, GroupChat, GroupMessage, MessageSenderType, Operator, SLAIncident
from app.utils import iso_z

logger = logging.getLogger("app.routers._common")

__all__ = [
    "operator_out",
    "chat_list_item",
    "message_out",
    "messages_out_with_reactions",
    "sla_snapshot",
    "sla_snapshot_dict",
    "sla_snapshot_full",
    "as_contract",
    "to_contract_dict",
    "iso_or_none",
    "safe_broadcast",
    "safe_broadcast_many",
    "local_envelope",
    "sqlstate_of",
    "not_found",
]

#: Shapes :func:`to_contract_dict` can build straight from an ORM row.
CONTRACT_SHAPES: tuple[str, ...] = ("operator", "chat_list_item", "message", "sla_snapshot")


# ---------------------------------------------------------------------------
# Primitive formatting
# ---------------------------------------------------------------------------


def iso_or_none(value: datetime | None) -> str | None:
    """Format an optional datetime as contract ISO-8601 UTC (``Z`` suffix)."""
    if value is None:
        return None
    return iso_z(value)


def _coerce_aware_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _seconds_until(deadline: datetime | None) -> int | None:
    """Whole seconds from now until ``deadline`` (negative once overdue)."""
    if deadline is None:
        return None
    delta = (_coerce_aware_utc(deadline) - datetime.now(timezone.utc)).total_seconds()
    return int(delta)


def _enum_str(value: Any) -> str:
    """Return the plain string of a (possibly) enum-typed column value."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def sqlstate_of(exc: BaseException) -> str | None:
    """Best-effort SQLSTATE extraction from a DBAPI/asyncpg exception.

    Lets routers detect unique-constraint races (``23505``) without importing a
    driver-specific exception class.
    """
    candidates: list[Any] = [exc, getattr(exc, "orig", None)]
    nested = getattr(getattr(exc, "orig", None), "orig", None)
    candidates.append(nested)
    for origin in candidates:
        if origin is None:
            continue
        code = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
        if code:
            return str(code)
        args = getattr(origin, "args", ())
        if args and isinstance(args[0], str):
            text = args[0]
            if "duplicate key value" in text.lower():
                return "23505"
            if text.strip().startswith("23"):
                return text.strip()[:5]
    return None


# ---------------------------------------------------------------------------
# Schema helper dispatch
# ---------------------------------------------------------------------------


def _schema_object(name: str) -> Any | None:
    """Return ``app.schemas.<name>`` when this build provides it."""
    try:
        from app import schemas as schema_module
    except Exception:  # pragma: no cover - schemas is a required peer module
        logger.debug("app.schemas is not importable; using contract dicts")
        return None
    return getattr(schema_module, name, None)


def _schema_fields(name: str) -> set[str]:
    """Field names declared by ``app.schemas.<name>`` (empty when unavailable)."""
    model = _schema_object(name)
    fields = getattr(model, "model_fields", None)
    return set(fields) if isinstance(fields, dict) else set()


def as_contract(schema_name: str, payload: Mapping[str, Any] | None) -> Any:
    """Return a FastAPI-ready body for ``schema_name`` built from ``payload``.

    The pydantic model is used when available and accepts the payload; otherwise
    the contract dict is returned verbatim, so a response never 500s because of
    a schema-side drift.
    """
    if payload is None:
        return None
    model = _schema_object(schema_name)
    if not (isinstance(model, type) and hasattr(model, "model_validate")):
        return dict(payload)
    try:
        return model.model_validate(dict(payload))
    except Exception as exc:  # noqa: BLE001 - the contract dict is authoritative
        logger.warning(
            "schemas.%s rejected the contract payload (%s); returning the dict",
            schema_name,
            exc.__class__.__name__,
        )
        return dict(payload)


def _dump(model: Any) -> dict[str, Any]:
    """Serialise any pydantic model to a JSON-ready dict."""
    dump_model = _schema_object("dump_model")
    if callable(dump_model):
        try:
            return dict(dump_model(model))
        except Exception as exc:  # noqa: BLE001
            logger.debug("dump_model failed (%s); using model_dump", exc)
    return dict(model.model_dump(mode="json", exclude_none=False))


# ---------------------------------------------------------------------------
# Contract dict builders (exact field names from CONTRACTS.md)
# ---------------------------------------------------------------------------


def to_contract_dict(shape: str, obj: Any, **extra: Any) -> dict[str, Any]:
    """Build the field-exact contract dict for ``shape`` from an ORM object.

    ``extra`` supplies joined values that are not columns on ``obj`` itself
    (e.g. ``assigned_operator_name``).
    """
    if shape == "operator":
        if not isinstance(obj, Operator):
            raise TypeError(f"operator shape expects Operator, got {type(obj).__name__}")
        return {
            "id": int(obj.id),
            "username": obj.username,
            "display_name": obj.display_name,
            "role": _enum_str(obj.role),
            "shift_status": _enum_str(obj.shift_status),
            "is_online_ws": bool(obj.is_online_ws),
            "last_seen_at": iso_or_none(obj.last_seen_at),
            "sla_target_seconds": int(obj.sla_target_seconds or 0),
            "created_at": iso_or_none(obj.created_at),
            # CONTRACTS_V5 §1.3: the permission flags travel with the operator so the UI
            # can hide/disable the delete control instead of discovering a 403.
            "can_delete_messages": bool(getattr(obj, "can_delete_messages", False)),
            "can_export": bool(getattr(obj, "can_export", False)),
        }

    if shape == "sla_snapshot":
        if not isinstance(obj, SLAIncident):
            raise TypeError(f"sla_snapshot shape expects SLAIncident, got {type(obj).__name__}")
        remaining = extra.get("remaining_seconds")
        if remaining is None:
            remaining = _seconds_until(obj.deadline_at) or 0
        is_overdue = extra.get("is_overdue")
        if is_overdue is None:
            is_overdue = int(remaining) < 0 and _enum_str(obj.status) == "PENDING"
        return {
            "chat_id": int(obj.chat_id),
            "incident_id": int(obj.id),
            "status": _enum_str(obj.status),
            "deadline_at": iso_or_none(obj.deadline_at),
            "remaining_seconds": int(remaining),
            "assigned_operator_id": obj.assigned_operator_id,
            "assigned_operator_name": extra.get("assigned_operator_name"),
            "is_overdue": bool(is_overdue),
        }

    if shape == "message":
        if not isinstance(obj, GroupMessage):
            raise TypeError(f"message shape expects GroupMessage, got {type(obj).__name__}")
        sender_type = _enum_str(obj.sender_type)
        return {
            "id": int(obj.id),
            "chat_id": int(obj.chat_id),
            "tg_msg_id": obj.tg_msg_id,
            "sender_type": sender_type,
            "sender_operator_id": obj.sender_operator_id,
            "sender_name": obj.sender_name,
            "sender_tg_id": obj.sender_tg_id,
            "text": obj.text,
            "reply_to_id": obj.reply_to_id,
            "reply_to_tg_msg_id": obj.reply_to_tg_msg_id,
            "phone": obj.phone,
            "is_deleted_in_tg": bool(obj.is_deleted_in_tg),
            "deleted_at": iso_or_none(obj.deleted_at),
            "sla_incident_id": obj.sla_incident_id,
            "delivery_status": _enum_str(obj.delivery_status),
            "is_outgoing": sender_type == MessageSenderType.OPERATOR.value,
            "created_at": iso_or_none(obj.created_at),
        }

    if shape == "chat_list_item":
        if not isinstance(obj, GroupChat):
            raise TypeError(f"chat_list_item shape expects GroupChat, got {type(obj).__name__}")
        last_message = extra.get("last_message")
        incident = extra.get("incident")
        assigned_name = extra.get("assigned_operator_name")
        return {
            "id": int(obj.id),
            "tg_chat_id": obj.tg_chat_id,
            "title": obj.title,
            "username": obj.username,
            "merchant_code": obj.merchant_code,
            "unread_count": int(obj.unread_count or 0),
            "assigned_operator_id": obj.assigned_operator_id,
            "assigned_operator_name": assigned_name,
            "last_message": (
                {
                    "id": int(last_message.id),
                    "text": last_message.text or "",
                    "sender_name": last_message.sender_name,
                    "is_outgoing": _enum_str(last_message.sender_type)
                    == MessageSenderType.OPERATOR.value,
                    "created_at": iso_or_none(last_message.created_at),
                }
                if last_message is not None
                else None
            ),
            "sla": (
                sla_snapshot_dict(incident, assigned_operator_name=assigned_name)
                if incident is not None
                else None
            ),
            "last_message_at": iso_or_none(obj.last_message_at),
        }

    raise ValueError(f"Unknown contract shape: {shape!r} (expected one of {CONTRACT_SHAPES})")


# ---------------------------------------------------------------------------
# Serialisers (canonical schemas builders first, contract dicts as fallback)
# ---------------------------------------------------------------------------


def operator_out(operator: Operator) -> Any:
    """Serialise an ``Operator`` as ``OperatorOut`` (§3.2)."""
    return as_contract("OperatorOut", to_contract_dict("operator", operator))


def message_out(message: GroupMessage) -> Any:
    """Serialise a ``GroupMessage`` as ``MessageOut`` (§3.3)."""
    schema_cls = _schema_object("MessageOut")
    builder = getattr(schema_cls, "from_orm_message", None)
    if callable(builder):
        try:
            return builder(message)
        except Exception as exc:  # noqa: BLE001 - fall back to the contract builder
            logger.warning(
                "MessageOut.from_orm_message failed for message %s (%s); using contract dict",
                getattr(message, "id", "?"),
                exc.__class__.__name__,
            )
    return as_contract("MessageOut", to_contract_dict("message", message))


async def messages_out_with_reactions(
    db: Any,
    messages: Iterable[GroupMessage],
    *,
    viewer_operator_id: int | None,
) -> list[Any]:
    """Serialise a whole page of messages, filling ``reactions`` in **two** queries.

    ``MessageOut.reactions`` cannot be filled from the ORM relationship: it needs
    distinct-operator counters (so one person reacting twice counts once) and the
    ``me`` flag of the caller. Doing that per message would be an ``N+1`` — for a
    100-message page, 100 aggregation queries. :func:`app.services.reactions.summarize_reactions`
    aggregates the whole page with one ``GROUP BY`` plus one ``LEFT JOIN``, so a page
    costs exactly two extra round trips whether it holds 1 or 500 messages.

    Attachment rows come from the already-loaded ``selectin`` relationship, so no
    per-message lazy IO (and no ``MissingGreenlet``) is risked here.
    """
    page = list(messages)
    serialised = [message_out(message) for message in page]
    if not page:
        return serialised

    try:
        from app.schemas import AttachmentOut, ReactionSummaryOut  # noqa: PLC0415 - peer module
        from app.services.reactions import summarize_reactions  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - history must render even without reactions
        logger.warning("reaction enrichment unavailable (%s: %s)", type(exc).__name__, exc)
        return serialised

    message_ids = [int(message.id) for message in page]
    try:
        mapping = await summarize_reactions(
            db, message_ids=message_ids, viewer_operator_id=viewer_operator_id
        )
    except Exception as exc:  # noqa: BLE001 - a counter failure must not break the page
        logger.warning("summarize_reactions failed for a page of %d: %s", len(message_ids), exc)
        return serialised

    enriched: list[Any] = []
    for message, payload in zip(page, serialised, strict=True):
        reactions = [
            ReactionSummaryOut.model_validate(item) for item in (mapping.get(int(message.id)) or [])
        ]
        attachments = [
            AttachmentOut.from_orm_attachment(row)
            for row in (getattr(message, "attachments", None) or [])
        ]
        if hasattr(payload, "model_copy"):
            enriched.append(
                payload.model_copy(update={"reactions": reactions, "attachments": attachments})
            )
        else:  # contract-dict fallback: keep the same JSON shape
            enriched.append(
                {
                    **payload,
                    "reactions": [item.model_dump(mode="json") for item in reactions],
                    "attachments": [item.model_dump(mode="json") for item in attachments],
                }
            )
    return enriched


def sla_snapshot_dict(incident: SLAIncident, *, assigned_operator_name: str | None = None) -> dict[str, Any]:
    """Build the ``SlaSnapshot`` dict (§3.3) computing the countdown server-side."""
    return to_contract_dict(
        "sla_snapshot",
        incident,
        remaining_seconds=_seconds_until(incident.deadline_at) or 0,
        assigned_operator_name=assigned_operator_name,
    )


def sla_snapshot(incident: SLAIncident, *, assigned_operator_name: str | None = None) -> dict[str, Any]:
    """Build the §3.3 ``SlaSnapshot`` payload as a plain contract dict.

    §3.3 freezes the object as ``{status, deadline_at, remaining_seconds,
    is_overdue}`` — the chat badge only needs the countdown. Returning a dict (not
    a pydantic instance) keeps it composable: callers wrap it in whichever
    envelope they declare (``SlaSnapshotOut``, ``ChatListItem.sla``, a WS event).
    The identifier-bearing variant used by ``SLA_UPDATE`` is
    :func:`sla_snapshot_full`.
    """
    payload = sla_snapshot_dict(incident, assigned_operator_name=assigned_operator_name)
    fields = _schema_fields("SlaSnapshot")
    if fields:
        payload = {key: value for key, value in payload.items() if key in fields}
    return payload


def sla_snapshot_full(incident: SLAIncident, *, assigned_operator_name: str | None = None) -> Any:
    """Serialise an ``SLAIncident`` with its identifiers (``ChatSlaOut`` / §4 ``SLA_UPDATE``)."""
    return as_contract(
        "ChatSlaOut",
        sla_snapshot_dict(incident, assigned_operator_name=assigned_operator_name),
    )


def chat_list_item(
    chat: GroupChat,
    *,
    last_message: GroupMessage | None = None,
    incident: SLAIncident | None = None,
    assigned_operator_name: str | None = None,
) -> Any:
    """Serialise a ``GroupChat`` (plus joins) as ``ChatListItem`` (§3.3).

    ``ChatListItem.build`` is the canonical builder; it derives the SLA snapshot
    via ``SlaSnapshot.from_incident`` and the trimmed ``last_message`` object.
    ``assigned_operator_name`` overrides the ORM-relationship lookup so a batched
    list only needs one extra query for the whole page.
    """
    schema_cls = _schema_object("ChatListItem")
    builder = getattr(schema_cls, "build", None)
    if callable(builder):
        try:
            item = builder(chat, last_message=last_message, sla=incident)
            payload = _dump(item)
            # ``ChatListItem.build`` falls back to ``Operator #<id>`` when the
            # relationship is not loaded (batch list queries never load it), so
            # the resolved name is always applied explicitly. It stays ``None``
            # only for a truly unassigned chat.
            payload["assigned_operator_name"] = assigned_operator_name
            payload["sla"] = (
                sla_snapshot_dict(incident, assigned_operator_name=assigned_operator_name)
                if incident is not None
                else None
            )
            return as_contract("ChatListItem", payload)
        except Exception as exc:  # noqa: BLE001 - fall back to the contract builder
            logger.warning(
                "ChatListItem.build failed for chat %s (%s); using contract dict",
                getattr(chat, "id", "?"),
                exc.__class__.__name__,
            )
    return as_contract(
        "ChatListItem",
        to_contract_dict(
            "chat_list_item",
            chat,
            last_message=last_message,
            incident=incident,
            assigned_operator_name=assigned_operator_name,
        ),
    )


def enum_variants(values: Iterable[Any]) -> list[str]:
    """Return every spelling (str value and enum member) of ``values``."""
    out: list[str] = []
    for value in values:
        raw = getattr(value, "value", value)
        text = str(raw)
        if text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Broadcast plumbing
# ---------------------------------------------------------------------------


def _envelope(event_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Build the §4 WS envelope through ``ws_manager.envelope``."""
    try:
        from app.ws_manager import envelope

        return envelope(event_type, dict(data))
    except Exception as exc:  # noqa: BLE001 - build locally on any problem
        logger.debug("ws_manager.envelope unavailable (%s); building locally", exc)
    return local_envelope(event_type, data)


def local_envelope(event_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Envelope built without importing ``ws_manager`` (used by WS-free tests)."""
    return {"type": event_type, "ts": iso_z(datetime.now(timezone.utc)), "data": dict(data)}


async def safe_broadcast(event_type: str, data: Mapping[str, Any]) -> bool:
    """Broadcast one WS envelope, never raising. Returns ``True`` on delivery."""
    try:
        from app.ws_manager import manager

        delivered = await manager.broadcast(_envelope(event_type, data))
        return bool(delivered)
    except Exception as exc:  # noqa: BLE001 - the HTTP response must survive
        logger.warning("WS broadcast of %s failed: %s: %s", event_type, type(exc).__name__, exc)
        return False


async def safe_broadcast_many(envelopes: list[tuple[str, Mapping[str, Any]]]) -> None:
    """Broadcast several envelopes sequentially, isolating each failure."""
    for event_type, data in envelopes:
        await safe_broadcast(event_type, data)


def not_found(detail: str = "Chat not found") -> HTTPException:
    """Uniform 404 for unknown chats."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


#: Delivery statuses in contract form, handy for routers building audit rows.
DELIVERY_STATUSES: tuple[str, ...] = tuple(status.value for status in DeliveryStatus)

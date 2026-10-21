"""Chats router (§3.3): list, history, read receipts, assignment, typing.

The chat list is assembled with a constant number of set-based queries (chats,
then last-message ids via one window function, then messages / open incidents /
operator names by id) instead of a per-chat ``N+1`` loop.

``GET /chats`` owns the canonical ``ChatListItem`` serializer helpers that
``messages.py`` re-uses when it attaches the refreshed chat to a
``NEW_MESSAGE`` broadcast.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import GroupChat, GroupMessage, Operator, OperatorRole, SLAIncident, SlaIncidentStatus
from app.routers._common import (
    _seconds_until,
    chat_list_item,
    message_out,
    messages_out_with_reactions,
    not_found,
    safe_broadcast,
)
from app.routers.envelopes import ChatListResponse
from app.schemas import AssignRequest, ChatListItem, ChatListOut, MessageListOut, ReadOut, TypingRequest
from app.security import get_current_operator
from app.services.typing import handle_typing_event

logger = logging.getLogger("app.routers.chats")

router = APIRouter(prefix="/chats", tags=["chats"])

ChatFilter = Literal["all", "mine", "burning"]

#: A pending incident is "burning" when this little time (or less) remains.
BURNING_THRESHOLD_SECONDS = 60
#: Default / maximum page size for the message history.
DEFAULT_MESSAGE_LIMIT = 100
MAX_MESSAGE_LIMIT = 500
#: Default / maximum number of chats returned.
DEFAULT_CHAT_LIMIT = 200
MAX_CHAT_LIMIT = 500


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def is_supervisor(operator: Operator) -> bool:
    """True when ``operator`` holds supervisor privileges."""
    return bool(getattr(operator, "is_supervisor", False)) or (
        str(getattr(operator.role, "value", operator.role)) == OperatorRole.SUPERVISOR.value
    )


async def get_chat(db: AsyncSession, chat_id: int) -> GroupChat:
    """Load one chat or raise ``404`` (shared by every chat/message route)."""
    chat = (await db.execute(select(GroupChat).where(GroupChat.id == chat_id))).scalar_one_or_none()
    if chat is None:
        raise not_found(f"Chat {chat_id} not found")
    return chat


async def operator_display_names(db: AsyncSession, operator_ids: set[int | None]) -> dict[int, str]:
    """Resolve ``{operator_id: display_name}`` with a single query."""
    clean = {int(value) for value in operator_ids if value}
    if not clean:
        return {}
    rows = (await db.execute(select(Operator.id, Operator.display_name).where(Operator.id.in_(clean)))).all()
    return {int(row[0]): str(row[1]) for row in rows}


async def open_incidents_for(db: AsyncSession, chat_ids: list[int]) -> dict[int, SLAIncident]:
    """Return ``{chat_id: most urgent PENDING incident}`` for the given chats.

    ``sla_engine`` guarantees at most one PENDING incident per chat; the extra
    ``setdefault`` keeps the badge deterministic if legacy data breaks that.
    """
    if not chat_ids:
        return {}
    rows = (
        await db.execute(
            select(SLAIncident)
            .where(
                SLAIncident.chat_id.in_(chat_ids),
                SLAIncident.status == SlaIncidentStatus.PENDING.value,
            )
            .order_by(SLAIncident.deadline_at.asc(), SLAIncident.id.desc())
        )
    ).scalars().all()
    latest: dict[int, SLAIncident] = {}
    for incident in rows:
        latest.setdefault(int(incident.chat_id), incident)
    return latest


def is_burning(incident: SLAIncident | None) -> bool:
    """True when a PENDING incident is overdue or inside the burning window."""
    if incident is None:
        return False
    remaining = _seconds_until(incident.deadline_at)
    if remaining is None:
        return False
    return remaining <= BURNING_THRESHOLD_SECONDS


def build_chat_list_items(
    chats: list[GroupChat],
    last_messages: dict[int, GroupMessage],
    incidents: dict[int, SLAIncident],
    names: dict[int, str],
) -> list[Any]:
    """Serialise ``ChatListItem`` objects from pre-fetched join data."""
    return [
        chat_list_item(
            chat,
            last_message=last_messages.get(int(chat.id)),
            incident=incidents.get(int(chat.id)),
            assigned_operator_name=names.get(int(chat.assigned_operator_id)) if chat.assigned_operator_id else None,
        )
        for chat in chats
    ]


async def last_message_for(db: AsyncSession, chat_id: int) -> GroupMessage | None:
    """Return the newest message of ``chat_id`` (or ``None``)."""
    return (
        await db.execute(
            select(GroupMessage)
            .where(GroupMessage.chat_id == chat_id)
            .order_by(GroupMessage.created_at.desc(), GroupMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def last_message_ids(db: AsyncSession, chat_ids: list[int]) -> dict[int, int]:
    """Return ``{chat_id: newest message id}`` in one windowed query."""
    if not chat_ids:
        return {}
    ranked = (
        select(
            GroupMessage.chat_id.label("chat_id"),
            GroupMessage.id.label("message_id"),
            func.row_number()
            .over(
                partition_by=GroupMessage.chat_id,
                order_by=(GroupMessage.created_at.desc(), GroupMessage.id.desc()),
            )
            .label("rn"),
        )
        .where(GroupMessage.chat_id.in_(chat_ids))
        .subquery()
    )
    rows = (await db.execute(select(ranked.c.chat_id, ranked.c.message_id).where(ranked.c.rn == 1))).all()
    return {int(row[0]): int(row[1]) for row in rows}


async def messages_by_ids(db: AsyncSession, message_ids: set[int]) -> dict[int, GroupMessage]:
    """Load the given messages by primary key."""
    clean = {int(value) for value in message_ids if value}
    if not clean:
        return {}
    rows = (await db.execute(select(GroupMessage).where(GroupMessage.id.in_(clean)))).scalars().all()
    return {int(row.id): row for row in rows}


async def load_chat_list_item(db: AsyncSession, chat_id: int) -> Any:
    """Load a single chat as a fully populated ``ChatListItem``.

    Re-used by ``POST /chats/{chat_id}/assign`` and by ``messages.py`` when it
    attaches the refreshed chat to the ``NEW_MESSAGE`` broadcast.
    """
    chat = await get_chat(db, chat_id)
    last_message = await last_message_for(db, chat_id)
    incidents = await open_incidents_for(db, [chat_id])
    names = await operator_display_names(db, {chat.assigned_operator_id})
    return build_chat_list_items(
        [chat], {chat_id: last_message} if last_message else {}, incidents, names
    )[0]


def _sort_key_remaining(incidents: dict[int, SLAIncident], item: Any) -> int:
    """Sorting key: remaining seconds of the chat's open incident (last when none)."""
    chat_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
    incident = incidents.get(int(chat_id)) if chat_id is not None else None
    remaining = _seconds_until(incident.deadline_at) if incident is not None else None
    return remaining if remaining is not None else 10**9


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ChatListOut,
    status_code=status.HTTP_200_OK,
    summary="List group chats (all | mine | burning)",
)
async def list_chats(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    chat_filter: Annotated[
        ChatFilter,
        Query(alias="filter", description="all = every chat, mine = assigned to me, burning = SLA at risk"),
    ] = "all",
    limit: Annotated[int, Query(ge=1, le=MAX_CHAT_LIMIT, description="Maximum chats returned")] = DEFAULT_CHAT_LIMIT,
) -> ChatListOut:
    """Return chats ordered by ``last_message_at`` desc; ``burning`` floats urgent ones first."""
    stmt = select(GroupChat)
    if chat_filter == "mine":
        stmt = stmt.where(GroupChat.assigned_operator_id == operator.id)

    chats = list(
        (
            await db.execute(
                stmt.order_by(
                    GroupChat.last_message_at.desc().nullslast(),
                    GroupChat.id.desc(),
                ).limit(limit)
            )
        ).scalars().all()
    )
    chat_ids = [int(chat.id) for chat in chats]

    incidents = await open_incidents_for(db, chat_ids)
    if chat_filter == "burning":
        chats = [chat for chat in chats if is_burning(incidents.get(int(chat.id)))]

    last_ids = await last_message_ids(db, [int(chat.id) for chat in chats])
    last_messages = await messages_by_ids(db, set(last_ids.values()))
    ordered_last = {chat_id: last_messages[msg_id] for chat_id, msg_id in last_ids.items() if msg_id in last_messages}

    names = await operator_display_names(db, {chat.assigned_operator_id for chat in chats})
    items = build_chat_list_items(chats, ordered_last, incidents, names)
    if chat_filter == "burning":
        items.sort(key=lambda item: _sort_key_remaining(incidents, item))

    logger.debug("chat list (%s) by operator %s: %s items", chat_filter, operator.id, len(items))
    return ChatListOut.model_validate({"items": items})


@router.get(
    "/{chat_id}/messages",
    response_model=MessageListOut,
    status_code=status.HTTP_200_OK,
    summary="Message history for one chat (ascending, cursor paginated)",
)
async def list_messages(
    chat_id: int,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_MESSAGE_LIMIT, description="Page size")] = DEFAULT_MESSAGE_LIMIT,
    before_id: Annotated[int | None, Query(description="Return messages older than this CRM message id")] = None,
) -> MessageListOut:
    """Return ``{"items": [MessageOut], "has_more": bool}`` ascending by ``created_at``."""
    await get_chat(db, chat_id)

    stmt = select(GroupMessage).where(GroupMessage.chat_id == chat_id)
    if before_id is not None:
        stmt = stmt.where(GroupMessage.id < before_id)

    # Fetch limit+1 newest rows: the extra row only proves another page exists.
    rows = list((await db.execute(stmt.order_by(GroupMessage.id.desc()).limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    page.reverse()  # ascending: ids are monotonic with created_at for append-only history

    logger.debug("message page for chat %s: %s rows (has_more=%s)", chat_id, len(page), has_more)
    # ``reactions`` / ``attachments`` are filled for the whole page with a fixed
    # number of queries (2 for reactions + the selectin loads), never per message.
    items = await messages_out_with_reactions(db, page, viewer_operator_id=operator.id)
    return MessageListOut.model_validate({"items": items, "has_more": has_more})


@router.post(
    "/{chat_id}/read",
    response_model=ReadOut,
    status_code=status.HTTP_200_OK,
    summary="Mark one chat as read",
)
async def mark_read(
    chat_id: int,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReadOut:
    """Reset ``unread_count`` to zero and return the new counter."""
    chat = await get_chat(db, chat_id)
    chat.unread_count = 0
    await db.commit()
    await db.refresh(chat)
    logger.debug("chat %s marked read by operator %s", chat_id, operator.id)
    return ReadOut.model_validate({"ok": True, "unread_count": int(chat.unread_count or 0)})


@router.post(
    "/{chat_id}/assign",
    response_model=ChatListItem,
    status_code=status.HTTP_200_OK,
    summary="Assign (or unassign) a chat to an operator",
    responses={
        403: {"description": "Only a supervisor may assign a chat to somebody else"},
        404: {"description": "Unknown chat or operator"},
    },
)
async def assign_chat(
    chat_id: int,
    payload: AssignRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    """Self-assign, or assign anybody when the caller is a supervisor."""
    chat = await get_chat(db, chat_id)
    target_id = payload.operator_id

    if target_id is not None:
        target = (await db.execute(select(Operator).where(Operator.id == int(target_id)))).scalar_one_or_none()
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Operator {target_id} not found",
            )

    if not is_supervisor(operator) and target_id != operator.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a supervisor can assign a chat to another operator",
        )

    chat.assigned_operator_id = int(target_id) if target_id is not None else None
    await db.commit()
    await db.refresh(chat)

    item = await load_chat_list_item(db, chat_id)
    item_dict = item if isinstance(item, dict) else item.model_dump(mode="json")
    await safe_broadcast(
        "CHAT_ASSIGNED",
        {
            "chat_id": int(chat.id),
            "assigned_operator_id": chat.assigned_operator_id,
            "assigned_operator_name": item_dict.get("assigned_operator_name"),
        },
    )
    logger.info("chat %s assigned to %s by operator %s", chat_id, chat.assigned_operator_id, operator.id)
    return item


@router.post(
    "/{chat_id}/typing",
    response_model=dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="Acquire or release the per-chat typing lock",
)
async def set_typing(
    chat_id: int,
    payload: TypingRequest,
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Delegate to ``typing.handle_typing_event`` and report the current holder.

    The service already broadcasts ``TYPING_LOCK`` / ``TYPING_UNLOCK``; this
    route only translates its verdict into the §3.5 body.
    """
    await get_chat(db, chat_id)

    action = str(getattr(payload.action, "value", payload.action)).lower()
    if action not in {"start", "stop"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid typing action: {action!r} (expected start or stop)",
        )

    result = await handle_typing_event(operator, chat_id, action)
    locked_by = result.get("locked_by") if isinstance(result, dict) else None
    # ``ok`` must mirror the service verdict: False means a *different* operator
    # already holds the lock, and the client must not treat that as success.
    granted = bool(result.get("ok", True)) if isinstance(result, dict) else True
    logger.debug(
        "typing %s on chat %s by operator %s -> ok=%s locked_by=%s",
        action,
        chat_id,
        operator.id,
        granted,
        locked_by,
    )
    return {"ok": granted, "locked_by": locked_by}

"""Reactions router (CONTRACTS_V2.md §3.2).

==================================================  =====================================
``PUT    /api/messages/{message_id}/reaction``      place or replace my reaction
``DELETE /api/messages/{message_id}/reaction``      take my reaction back
``GET    /api/messages/{message_id}/reactions``     aggregated counters (``me`` flag)
==================================================  =====================================

All three answer ``ReactionListOut`` — the freshly aggregated state of the
message — so the client can render the message directly from the response
without a second round trip. The service broadcasts ``MESSAGE_REACTION`` /
``MESSAGE_REACTION_REMOVED`` for every other connected tab.

Error contract:

* ``404`` — the message does not exist;
* ``422`` — the emoji is not one of the six supported reactions;
* ``401`` — missing/invalid bearer token (shared ``get_current_operator``).

Authentication reuses :data:`app.security.get_current_operator`, exactly like
``app.routers.messages`` / ``app.routers.chats``, so all routers agree on who is
calling. The module itself does not mount: ``app.main`` registers it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Operator
from app.schemas import ReactionListOut, ReactionRequest
from app.security import get_current_operator
from app.services.reactions import (
    MessageReactionNotFoundError,
    ReactionValidationError,
    clear_reaction,
    message_exists,
    set_reaction,
    summarize_reactions,
)

logger = logging.getLogger("tgdesk.routers.reactions")

router = APIRouter(tags=["reactions"])

#: Documented error bodies for the OpenAPI schema (§3.2).
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Сообщение не найдено"}}
_NOT_FOUND_OR_INVALID: dict[int | str, dict[str, Any]] = {
    404: {"description": "Сообщение не найдено"},
    422: {"description": "Недопустимый эмодзи реакции"},
}

#: Shared dependency aliases — identical to ``routers/messages.py``.
CurrentOperator = Annotated[Operator, Depends(get_current_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(message_id: int) -> HTTPException:
    """Uniform ``404`` for an unknown message."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Сообщение {int(message_id)} не найдено",
    )


def _validation_error(message: str) -> HTTPException:
    """Uniform ``422`` for a rejected emoji."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


async def _current_state(
    db: AsyncSession,
    *,
    message_id: int,
    operator: Operator,
) -> ReactionListOut:
    """Aggregated reactions of ``message_id`` from the caller's point of view.

    A missing message is reported as ``404`` instead of an empty list, so the UI
    can drop a stale bubble rather than rendering a counter row forever.

    Only the ``GET`` route calls this. ``PUT`` and ``DELETE`` answer with the
    aggregate the service already read inside its own write transaction, so they
    pay for **no** extra round trip — previously ``DELETE`` re-ran
    ``message_exists`` + ``summarize_reactions`` on top of the service's work.
    """
    if not await message_exists(db, message_id):
        raise _not_found(message_id)
    mapping = await summarize_reactions(
        db, message_ids=[int(message_id)], viewer_operator_id=int(operator.id)
    )
    return ReactionListOut(items=mapping.get(int(message_id), []))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.put(
    "/messages/{message_id}/reaction",
    response_model=ReactionListOut,
    status_code=status.HTTP_200_OK,
    summary="Поставить или сменить реакцию на сообщении",
    responses=_NOT_FOUND_OR_INVALID,
)
async def put_reaction(
    message_id: int,
    payload: ReactionRequest,
    operator: CurrentOperator,
    db: DatabaseSession,
) -> ReactionListOut:
    """Place ``payload.emoji`` as the caller's only reaction on the message.

    A repeated ``PUT`` with a different emoji **replaces** the previous one: the
    service deletes every row the operator already owns on this message before
    inserting, because the database's partial unique indexes include ``emoji``
    and would otherwise allow two reactions from one person.
    """
    try:
        result = await set_reaction(db, message_id=message_id, operator=operator, emoji=payload.emoji)
    except MessageReactionNotFoundError as exc:
        raise _not_found(message_id) from exc
    except ReactionValidationError as exc:
        raise _validation_error(str(exc)) from exc

    logger.debug(
        "operator %s put a reaction on message %s (%d emoji bucket(s))",
        operator.id,
        message_id,
        len(result.items),
    )
    return result


@router.delete(
    "/messages/{message_id}/reaction",
    response_model=ReactionListOut,
    status_code=status.HTTP_200_OK,
    summary="Снять свою реакцию с сообщения",
    responses=_NOT_FOUND,
)
async def delete_reaction(
    message_id: int,
    operator: CurrentOperator,
    db: DatabaseSession,
) -> ReactionListOut:
    """Remove **all** reactions the caller holds on the message.

    Idempotent: when the operator had nothing to remove the route still answers
    ``200`` with the current aggregate, so a retried ``DELETE`` never 404s.

    The aggregate comes straight from the service, which read it inside the same
    transaction as the ``DELETE`` — the route issues **no** second ``SELECT`` (it
    used to run ``message_exists`` + ``summarize_reactions`` again) and therefore
    cannot answer with a state older than its own write.
    """
    try:
        outcome = await clear_reaction(db, message_id=message_id, operator=operator)
    except MessageReactionNotFoundError as exc:
        raise _not_found(message_id) from exc
    except ReactionValidationError as exc:
        raise _validation_error(str(exc)) from exc

    logger.debug(
        "operator %s removed reaction(s) on message %s (changed=%s)",
        operator.id,
        message_id,
        outcome.removed,
    )
    return outcome.reactions


@router.get(
    "/messages/{message_id}/reactions",
    response_model=ReactionListOut,
    status_code=status.HTTP_200_OK,
    summary="Агрегированные реакции сообщения",
    responses=_NOT_FOUND,
)
async def get_reactions(
    message_id: int,
    operator: CurrentOperator,
    db: DatabaseSession,
) -> ReactionListOut:
    """Return every emoji bucket with its counter and the caller's ``me`` flag.

    A message without reactions answers ``{"items": []}`` in two queries at
    most, regardless of how many emoji it carries.
    """
    result = await _current_state(db, message_id=message_id, operator=operator)
    logger.debug(
        "operator %s read %d emoji bucket(s) on message %s",
        operator.id,
        len(result.items),
        message_id,
    )
    return result

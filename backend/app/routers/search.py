"""Global search router (CONTRACTS_V5 §6.2).

One endpoint::

    GET /api/search?q=<строка>&chat_id=&sender=all|client|operator
                    &date_from=&date_to=&limit=20&offset=0&include_deleted=false
        -> 200 SearchResponseOut

``q`` is required and must be 1..200 characters; an empty or over-long needle is
rejected with ``422`` before any SQL runs. Authentication reuses the shared
:data:`app.security.get_current_operator` dependency, exactly like
``app.routers.chats`` / ``app.routers.messages`` / ``app.routers.reactions``, so a
missing or invalid bearer token yields ``401``.

The heavy lifting (ILIKE escaping, ranking, snippet cutting) lives in
:mod:`app.services.search`; this module only validates the query string, times the
request and serialises the hits into ``SearchHitOut``.

``message`` is a full ``MessageOut`` produced by ``app.routers._common.message_out``
— the same serialiser ``GET /api/chats/{id}/messages`` uses — so a result can be
rendered by the existing message bubble without a second request.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Operator
from app.routers._common import message_out, messages_out_with_reactions
from app.schemas import MessageOut, SearchHitOut, SearchResponseOut
from app.security import get_current_operator
from app.services.search import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_OFFSET,
    MAX_SEARCH_QUERY_LENGTH,
    search_messages,
)

logger = logging.getLogger("tgdesk.routers.search")

router = APIRouter(tags=["search"])

#: Shared dependency aliases — identical to ``routers/chats.py``.
CurrentOperator = Annotated[Operator, Depends(get_current_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]

#: ``sender`` values accepted by §6.2.
SenderScope = Literal["all", "client", "operator"]


def _search_error(detail: str) -> HTTPException:
    """``422`` carrying a plain-language message for an unusable query."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


@router.get(
    "/search",
    response_model=SearchResponseOut,
    summary="Глобальный поиск по сообщениям",
    responses={
        401: {"description": "Требуется аутентификация оператора"},
        422: {"description": "Некорректные параметры поиска"},
    },
)
async def search(
    db: DatabaseSession,
    operator: CurrentOperator,  # noqa: ARG001 - presence enforces authentication
    q: Annotated[str, Query(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH, description="Строка поиска")],
    chat_id: Annotated[int | None, Query(ge=1, description="Искать только в этом чате")] = None,
    sender: Annotated[SenderScope, Query(description="client | operator | all")] = "all",
    date_from: Annotated[datetime | None, Query(description="Нижняя граница created_at")] = None,
    date_to: Annotated[datetime | None, Query(description="Верхняя граница created_at")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_SEARCH_OFFSET)] = 0,
    include_deleted: Annotated[bool, Query(description="Включать удалённые в Telegram")] = False,
) -> SearchResponseOut:
    """Find messages by substring across text, caption, sender, phone and chat title.

    Ranking is ``similarity()`` DESC then ``created_at`` DESC on PostgreSQL;
    ``created_at`` DESC alone on SQLite. Each hit carries a server-cut ``snippet``
    plus ``match_spans`` offsets relative to that snippet.
    """
    started = time.perf_counter()

    needle = q.strip()
    if not needle:
        # ``min_length=1`` already rejects ""; this catches a whitespace-only q,
        # which would otherwise degrade to "match everything".
        raise _search_error("Параметр q не может быть пустым")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise _search_error("date_from не может быть позже date_to")

    try:
        hits, total = await search_messages(
            db,
            q=needle,
            chat_id=chat_id,
            sender=sender,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            include_deleted=bool(include_deleted),
        )
    except ValueError as exc:
        # Raised by ``normalize_query`` for an empty / over-long needle.
        raise _search_error(str(exc)) from exc

    items: list[SearchHitOut] = []
    if hits:
        # One batched pass fills ``reactions``/``attachments`` for the whole page
        # (two extra queries total, never one per hit).
        raw_messages = [hit["message"] for hit in hits]
        serialised: list[Any]
        try:
            serialised = await messages_out_with_reactions(
                db, raw_messages, viewer_operator_id=int(getattr(operator, "id", 0) or 0)
            )
        except Exception as exc:  # noqa: BLE001 - a reaction hiccup must not break search
            logger.warning("reaction enrichment failed in search (%s)", type(exc).__name__)
            serialised = [message_out(message) for message in raw_messages]

        for hit, payload in zip(hits, serialised, strict=True):
            message = (
                payload if isinstance(payload, MessageOut) else MessageOut.model_validate(payload)
            )
            items.append(
                SearchHitOut(
                    chat_id=int(hit["chat_id"]),
                    chat_title=str(hit["chat_title"]),
                    chat_merchant_code=hit["chat_merchant_code"],
                    message=message,
                    snippet=str(hit["snippet"]),
                    match_spans=[list(span) for span in hit["match_spans"]],
                    score=float(hit["score"]),
                )
            )

    took_ms = int(round((time.perf_counter() - started) * 1000))
    return SearchResponseOut(
        items=items,
        total=int(total),
        has_more=(offset + len(items)) < int(total),
        took_ms=took_ms,
    )

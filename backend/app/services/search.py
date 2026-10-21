"""Global message search (CONTRACTS_V5 §6.2).

Search is a **case-insensitive substring** match (invariant §8.8): typing ``5607``
must find ``+1792560788`` and typing ``payo`` must find ``payout``. The backend
therefore uses ``ILIKE '%needle%'`` over five text carriers rather than full-text
search, and PostgreSQL accelerates it with the trigram GIN indexes created by
:mod:`migrate_v3_search` (§6.1).

Where the needle is matched
---------------------------
==================  ============================================================
``group_messages``  ``text``, ``caption``, ``sender_name``, ``phone``
``group_chats``     ``title``, ``merchant_code`` — joined, so a merchant is found
                    by its group name *or* by its code (``MRC-ALPHA``)
==================  ============================================================

Escaping is critical
--------------------
``ILIKE`` treats ``%`` and ``_`` as wildcards and ``\\`` as the escape character. A
user typing ``100%`` or ``file_name`` must search for those literal characters, not
for "anything" / "any single character" — an unescaped ``%`` would match every row
in the table. :func:`escape_like` therefore doubles the backslash first and then
escapes ``%`` and ``_``; :func:`like_contains_pattern` wraps the result in ``%…%``.
The same function guarantees the trigram index sees a constant prefix, so the
pattern stays ``p like $1`` and is index-friendly.

Ranking
-------
On PostgreSQL the results are ordered by ``similarity()`` of the needle against the
message body (``text``, falling back to ``caption``) descending, then by
``created_at`` descending, then by ``id`` descending for a stable page. SQLite has
no ``similarity()``, so the ranking degrades to ``created_at DESC`` — which is what
``test_*`` runs and any local dev database see.

Snippets are built **server-side** (:func:`make_snippet`): the browser receives one
short string plus the exact ``[start, end)`` offsets of the match inside it, so it
never has to re-implement the matching rules.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import Select, and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GroupChat, GroupMessage, MessageSenderType

logger = logging.getLogger("tgdesk.services.search")

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_QUERY_LENGTH",
    "SNIPPET_WIDTH",
    "SenderFilter",
    "escape_like",
    "like_contains_pattern",
    "make_snippet",
    "normalize_query",
    "search_messages",
]

#: ``sender`` filter accepted by the API (§6.2).
SenderFilter = Literal["all", "client", "operator"]

#: ``q`` length bounds (§6.2: 1..200 characters).
MAX_SEARCH_QUERY_LENGTH: Final[int] = 200

#: Paging bounds. ``limit`` is capped so one request can never stream the whole table.
DEFAULT_SEARCH_LIMIT: Final[int] = 20
MAX_SEARCH_LIMIT: Final[int] = 200
MAX_SEARCH_OFFSET: Final[int] = 100_000

#: Characters returned around the match by :func:`make_snippet` (§6.2: ~120).
SNIPPET_WIDTH: Final[int] = 120

#: Ellipsis marking a trimmed edge of a snippet (matches the §6.2 example).
ELLIPSIS: Final[str] = "…"

#: SQL ``LIKE``/``ILIKE`` escape character, spelled ``E'\\\\'`` in the generated SQL.
BACKSLASH: Final[str] = "\\"


# ---------------------------------------------------------------------------
# Query normalisation and LIKE escaping
# ---------------------------------------------------------------------------


def normalize_query(raw: str | None) -> str:
    """Trim ``raw`` and validate its length (§6.2: 1..200 characters).

    Returns the trimmed needle. Raises :class:`ValueError` when the needle is
    empty or longer than :data:`MAX_SEARCH_QUERY_LENGTH`; the router translates
    that into ``422``, and the service itself also refuses to run such a query.
    """
    if raw is None:
        raise ValueError("q is required")
    needle = str(raw).strip()
    if not needle:
        raise ValueError("q must not be empty")
    if len(needle) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError(f"q must be at most {MAX_SEARCH_QUERY_LENGTH} characters")
    return needle


def escape_like(value: str) -> str:
    """Escape ``LIKE``/``ILIKE`` metacharacters so ``value`` matches literally.

    The backslash is escaped **first**; reversing the order would double-escape the
    backslashes introduced for ``%`` and ``_`` and silently change the needle.
    ``escape_like("100%")`` yields ``"100\\\\%"``, which SQL reads as a literal
    percent sign rather than "any suffix".
    """
    return (
        value.replace(BACKSLASH, BACKSLASH * 2)
        .replace("%", f"{BACKSLASH}%")
        .replace("_", f"{BACKSLASH}_")
    )


def like_contains_pattern(needle: str) -> str:
    """``'%<escaped needle>%'`` — the substring pattern for ``ILIKE ... ESCAPE``."""
    return f"%{escape_like(needle)}%"


# ---------------------------------------------------------------------------
# Snippet construction
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Case-fold ``text`` **per character**, preserving character offsets.

    ``str.lower()`` can change the string length for a handful of code points
    (e.g. ``İ``), which would desynchronise the offsets returned by
    :func:`make_snippet` from the original string. Folding character by character
    guarantees ``len(_fold(s)) == len(s)`` for any input, so an offset found in the
    folded haystack is valid in the original.
    """
    folded: list[str] = []
    for char in text:
        lowered = char.lower()
        folded.append(lowered if len(lowered) == 1 else char)
    return "".join(folded)


def find_spans(text: str, needle: str) -> list[list[int]]:
    """Every non-overlapping ``[start, end)`` occurrence of ``needle`` in ``text``.

    Matching is case-insensitive via :func:`_fold`, and offsets refer to the
    **original** ``text``. Overlapping matches are skipped by advancing past the
    end of the previous hit, so ``"aaa"`` searched for ``"aa"`` yields one span.
    """
    if not text or not needle:
        return []
    haystack = _fold(text)
    target = _fold(needle)
    spans: list[list[int]] = []
    cursor = 0
    while True:
        index = haystack.find(target, cursor)
        if index < 0:
            return spans
        end = index + len(target)
        spans.append([index, end])
        cursor = end
    return spans


def make_snippet(text: str, needle: str, width: int = SNIPPET_WIDTH) -> tuple[str, list[list[int]]]:
    """Cut a ``width``-character window around the first match of ``needle``.

    Returns ``(snippet, spans)`` where ``spans`` are ``[start, end)`` offsets of the
    match **inside the returned snippet** — the client highlights them directly.

    Rules
    -----
    * No match (e.g. the hit came from ``phone`` or the chat ``title``, not from the
      body): the leading ``width`` characters are returned and ``spans`` is empty.
    * The window is centred on the first match, clamped to the string bounds, so
      short texts come back whole.
    * A trimmed edge is marked with :data:`ELLIPSIS` (U+2026) and the offsets are
      shifted accordingly, so highlighting always lands on the visible characters.
    * ``width`` is clamped to at least the needle length, otherwise the match could
      never fit inside the returned window.

    The returned snippet has no surrounding whitespace (the window edges are
    stripped), matching the ``"…найдено слово…"`` shape in §6.2.
    """
    width = max(int(width), len(needle), 1)
    source = text or ""

    spans = find_spans(source, needle)
    if not spans:
        trimmed = source[:width].strip()
        return trimmed, []

    start, end = spans[0]
    # Centre the window on the match, then clamp into the string.
    pad = max((width - (end - start)) // 2, 0)
    window_start = max(start - pad, 0)
    window_end = min(window_start + width, len(source))
    # If clamping the end left the window short, grow it towards the start.
    window_start = max(window_end - width, 0)

    prefix = ELLIPSIS if window_start > 0 else ""
    suffix = ELLIPSIS if window_end < len(source) else ""
    body = source[window_start:window_end]

    # Strip the edges for display and shift the offsets by what was removed.
    stripped_left = len(body) - len(body.lstrip())
    stripped_right = len(body) - len(body.rstrip())
    body = body.strip()
    shift = len(prefix) - stripped_left

    # Bring each occurrence from source coordinates into snippet coordinates:
    # ``-window_start`` locates it inside the window, ``+shift`` accounts for the
    # ellipsis prefix and for the whitespace stripped off the left edge.
    local_spans: list[list[int]] = []
    body_end = len(body)
    for span_start, span_end in spans:
        if span_end > window_end:
            continue  # this occurrence runs past the visible window
        local_start = span_start - window_start + shift
        local_end = span_end - window_start + shift
        if local_end <= 0 or local_start >= body_end:
            continue  # entirely in the trimmed/ellipsis area
        local_spans.append([max(local_start, 0), min(local_end, body_end)])

    if stripped_right:
        suffix = suffix or ""
    snippet = f"{prefix}{body}{suffix}"
    return snippet, local_spans


# ---------------------------------------------------------------------------
# SQL construction
# ---------------------------------------------------------------------------


def _is_postgresql(db: AsyncSession) -> bool:
    """True when the session is bound to PostgreSQL (enables ``similarity()``)."""
    try:
        bind = db.get_bind()
    except Exception:  # noqa: BLE001 - unbound/mocked sessions fall back to SQLite rules
        return False
    name = getattr(getattr(bind, "dialect", None), "name", "")
    return str(name).lower() == "postgresql"


def _sender_clause(sender: str) -> Any | None:
    """SQL predicate for ``sender`` ∈ {``all``, ``client``, ``operator``}.

    ``client`` covers everything that is *not* an operator reply (``CLIENT``,
    ``BOT``, ``SYSTEM``) — searching "client messages" must not silently hide the
    merchant's bot notifications. ``operator`` is the CRM side only.
    ``all``/unknown values add no predicate.
    """
    normalized = str(sender or "all").strip().lower()
    if normalized == "operator":
        return GroupMessage.sender_type == MessageSenderType.OPERATOR.value
    if normalized == "client":
        return GroupMessage.sender_type != MessageSenderType.OPERATOR.value
    return None


def _build_match_clause(needle: str) -> Any:
    """``OR`` over the six indexed text carriers (§6.2).

    ``GroupChat.merchant_code`` is included because ``MRC-ALPHA`` is the operator's
    primary working identifier for a merchant (§6.2 уточнение). Both chat columns
    live on the already-joined ``GroupChat``, so this adds no extra JOIN and can
    never duplicate a row: one ``GroupMessage`` yields exactly one
    ``(GroupMessage, GroupChat)`` pair, and every predicate is OR-ed inside a single
    ``WHERE`` — duplicates are impossible by construction.
    """
    pattern = like_contains_pattern(needle)
    return or_(
        GroupMessage.text.ilike(pattern, escape=BACKSLASH),
        GroupMessage.caption.ilike(pattern, escape=BACKSLASH),
        GroupMessage.sender_name.ilike(pattern, escape=BACKSLASH),
        GroupMessage.phone.ilike(pattern, escape=BACKSLASH),
        GroupChat.title.ilike(pattern, escape=BACKSLASH),
        GroupChat.merchant_code.ilike(pattern, escape=BACKSLASH),
    )


def _build_filters(
    needle: str,
    *,
    chat_id: int | None,
    sender: str,
    date_from: datetime | None,
    date_to: datetime | None,
    include_deleted: bool,
) -> list[Any]:
    """Every ``AND``-ed predicate for one search request."""
    filters: list[Any] = [_build_match_clause(needle)]

    if not include_deleted:
        filters.append(GroupMessage.is_deleted_in_tg.is_(False))
    if chat_id is not None:
        filters.append(GroupMessage.chat_id == int(chat_id))
    if date_from is not None:
        filters.append(GroupMessage.created_at >= date_from)
    if date_to is not None:
        filters.append(GroupMessage.created_at <= date_to)

    sender_clause = _sender_clause(sender)
    if sender_clause is not None:
        filters.append(sender_clause)

    return filters


def _body_for_snippet(message: GroupMessage, needle: str) -> str:
    """Text the snippet is cut from: the body, else the caption, else fields.

    A hit on ``phone`` / ``sender_name`` / ``title`` still needs a readable line, so
    the fallback chain ends at those columns instead of returning an empty snippet.
    """
    text = (getattr(message, "text", None) or "").strip()
    if text:
        return text
    caption = (getattr(message, "caption", None) or "").strip()
    if caption:
        return caption
    for attribute in ("phone", "sender_name"):
        value = (getattr(message, attribute, None) or "").strip()
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def search_messages(
    db: AsyncSession,
    *,
    q: str,
    chat_id: int | None = None,
    sender: str = "all",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    include_deleted: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Search messages by substring and return ``(page, total)``.

    Parameters
    ----------
    db:
        Request-scoped async session.
    q:
        Needle, 1..200 characters after trimming. ``%``/``_``/``\\`` are matched
        literally (:func:`escape_like`).
    chat_id:
        Restrict to one chat; ``None`` searches every chat.
    sender:
        ``client`` / ``operator`` / ``all`` (see :func:`_sender_clause`).
    date_from, date_to:
        Inclusive bounds on ``GroupMessage.created_at``.
    limit, offset:
        Paging. ``limit`` is clamped to ``[1, MAX_SEARCH_LIMIT]`` and ``offset`` to
        ``[0, MAX_SEARCH_OFFSET]``.
    include_deleted:
        When ``False`` (default) rows with ``is_deleted_in_tg = True`` are excluded.

    Returns
    -------
    tuple[list[dict], int]
        ``page`` — one dict per hit with ``chat_id``, ``chat_title``,
        ``chat_merchant_code``, ``message`` (the ORM row, serialised by the router),
        ``snippet``, ``match_spans`` and ``score``. ``total`` is the number of rows
        matching the filters regardless of paging, so the client can show
        "N results" and compute ``has_more``.

    Raises
    ------
    ValueError
        If ``q`` is empty or longer than :data:`MAX_SEARCH_QUERY_LENGTH`. The router
        turns this into ``422``.
    """
    needle = normalize_query(q)

    safe_limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    safe_offset = max(0, min(int(offset), MAX_SEARCH_OFFSET))
    postgres = _is_postgresql(db)

    filters = _build_filters(
        needle,
        chat_id=chat_id,
        sender=sender,
        date_from=date_from,
        date_to=date_to,
        include_deleted=include_deleted,
    )
    where_clause = and_(*filters)

    # ---- total ------------------------------------------------------------
    total_stmt: Select[Any] = (
        select(func.count())
        .select_from(GroupMessage)
        .join(GroupChat, GroupChat.id == GroupMessage.chat_id)
        .where(where_clause)
    )
    total = int((await db.execute(total_stmt)).scalar_one() or 0)
    if total == 0:
        return [], 0

    # ---- page -------------------------------------------------------------
    # ``score`` is the §6.2 ranking signal. On PostgreSQL it is the trigram
    # similarity of the needle against the message body; SQLite has no
    # ``similarity()``, so a constant 0.0 is selected and ordering falls back to
    # ``created_at DESC``. ``coalesce`` keeps caption-only (media) hits rankable.
    if postgres:
        score_expr: Any = func.coalesce(
            func.similarity(func.coalesce(GroupMessage.text, literal("")), needle),
            func.similarity(func.coalesce(GroupMessage.caption, literal("")), needle),
            literal(0.0),
        )
    else:
        score_expr = literal(0.0)

    page_stmt = (
        select(GroupMessage, GroupChat, score_expr.label("score"))
        .join(GroupChat, GroupChat.id == GroupMessage.chat_id)
        .where(where_clause)
        .order_by(score_expr.desc(), GroupMessage.created_at.desc(), GroupMessage.id.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    )

    rows: Sequence[Any] = (await db.execute(page_stmt)).all()

    items: list[dict[str, Any]] = []
    for row in rows:
        message: GroupMessage = row[0]
        chat: GroupChat = row[1]
        raw_score = row[2]
        body = _body_for_snippet(message, needle)
        snippet, spans = make_snippet(body, needle)
        try:
            score = float(raw_score) if raw_score is not None else 0.0
        except (TypeError, ValueError):  # pragma: no cover - defensive
            score = 0.0
        items.append(
            {
                "chat_id": int(chat.id),
                "chat_title": chat.title,
                "chat_merchant_code": chat.merchant_code,
                "message": message,
                "snippet": snippet,
                "match_spans": spans,
                "score": round(score, 6),
            }
        )

    return items, total

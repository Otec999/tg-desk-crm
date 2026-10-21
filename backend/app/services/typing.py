"""Per-chat collision lock ("who is typing right now") backed by Redis.

CONTRACTS.md §3.5 / §5:

* ``POST /api/chats/{chat_id}/typing`` ``{"action": "start" | "stop"}`` ->
  ``{"ok": true, "locked_by": "<display_name or null>"}``;
* the lock lives in the Redis key ``typing_lock:{chat_id}``, value = operator id, TTL 15 s;
* it is refreshed by repeated ``start``, released on ``stop`` / successful send / WS disconnect;
* ``TYPING_LOCK`` / ``TYPING_UNLOCK`` envelopes fan out so every client sees the collision.

Degradation contract: **Redis being down must never block an operator.** Every coroutine here is
defensive — a transport error is logged and turned into the permissive answer
(``{"ok": True, "locked_by": None}``), because a false "someone else is typing" hides a chat from
the desk, while a missed lock only costs a duplicate reply that the operator can still see.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import select

from app.models import Operator
from app.redis_bus import (
    TYPING_LOCK_DEFAULT_TTL,
    clear_typing_lock,
    get_typing_lock,
    set_typing_lock,
)
from app.utils import iso_z, utcnow
from app.ws_manager import (
    EVT_TYPING_LOCK,
    EVT_TYPING_UNLOCK,
    envelope,
    manager,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TYPING_LOCK_TTL_SECONDS",
    "describe_lock",
    "get_lock_info",
    "handle_typing_event",
    "operator_display_name",
    "release_all_locks_for_operator",
    "release_chat_lock",
]

#: Lock lifetime in seconds (CONTRACTS.md §3.5: 15 s, refreshed by repeated starts).
TYPING_LOCK_TTL_SECONDS: Final[int] = TYPING_LOCK_DEFAULT_TTL

#: Actions accepted by :func:`handle_typing_event`.
_ACTIONS: Final[frozenset[str]] = frozenset({"start", "stop"})


# ============================================================================ helpers
def _operator_id(operator: Any) -> int | None:
    """``operator.id`` as an int (``None`` when the object carries no id)."""
    try:
        return int(getattr(operator, "id"))
    except (AttributeError, TypeError, ValueError):
        return None


def _display_name(operator: Any) -> str:
    """Best-effort display label for ``operator`` (never empty)."""
    for attribute in ("display_name", "username"):
        value = getattr(operator, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    operator_id = _operator_id(operator)
    return f"Operator #{operator_id}" if operator_id is not None else "Unknown operator"


async def operator_display_name(operator_id: int, *, session_factory: Any = None) -> str | None:
    """Resolve ``operator_id`` -> ``display_name`` from the database, or ``None``.

    Uses ``app.database.SessionLocal`` unless a ``session_factory`` is supplied. Never raises:
    the lock payload is cosmetic, a missing name must not fail the typing request.
    """
    session = None
    try:
        factory = session_factory
        if factory is None:
            from app.database import SessionLocal

            factory = SessionLocal
        session = factory()
        name = (
            await session.execute(
                select(Operator.display_name).where(Operator.id == int(operator_id))
            )
        ).scalar_one_or_none()
        return str(name) if name else None
    except Exception as exc:  # noqa: BLE001 - name resolution is best-effort
        logger.debug("operator_display_name(%s) failed: %s: %s", operator_id, type(exc).__name__, exc)
        return None
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("operator_display_name: session close failed: %s", exc)


async def get_lock_info(chat_id: int, *, session_factory: Any = None) -> dict[str, Any] | None:
    """Current owner of the typing lock for ``chat_id``.

    Returns ``{"operator_id": int, "operator_name": str | None, "until": iso-str}`` or ``None``
    when the chat is free / Redis is unreachable. ``until`` is the best-known expiry: the Redis
    TTL is read when the client supports it, otherwise ``now + TYPING_LOCK_TTL_SECONDS``.
    """
    try:
        holder = await get_typing_lock(int(chat_id))
    except Exception as exc:  # noqa: BLE001 - Redis down means "free"
        logger.warning("get_lock_info(%s): %s: %s", chat_id, type(exc).__name__, exc)
        return None
    if holder is None:
        return None
    return {
        "operator_id": int(holder),
        "operator_name": await operator_display_name(int(holder), session_factory=session_factory),
        "until": iso_z(utcnow() + timedelta(seconds=TYPING_LOCK_TTL_SECONDS)),
    }


# ============================================================================ broadcast plumbing
async def _broadcast(event_type: str, data: dict[str, Any]) -> None:
    """Fan out one envelope; transport failures are logged, never raised."""
    try:
        await manager.broadcast(envelope(event_type, data))
    except Exception as exc:  # noqa: BLE001 - the REST response must survive
        logger.warning("broadcast of %s failed: %s: %s", event_type, type(exc).__name__, exc)


# ============================================================================ public API
async def handle_typing_event(operator: Operator, chat_id: int, action: str) -> dict[str, Any]:
    """Handle a ``start``/``stop`` typing event for ``chat_id`` on behalf of ``operator``.

    ``start``:
        * free chat, or already held by this operator -> (re)acquire with a 15 s TTL and broadcast
          ``TYPING_LOCK`` ``{chat_id, operator_id, operator_name, until}`` -> ``ok=True``;
        * held by somebody else -> **no** write, broadcast the same ``TYPING_LOCK`` (so every
          client refreshes the owner badge) -> ``{"ok": False, "locked_by": <display_name>}``.

    ``stop``:
        * release (only the owner can) and broadcast ``TYPING_UNLOCK`` ``{chat_id, operator_id}``.

    Returns: ``{"ok": bool, "locked_by": str | None, "chat_id": int}``. Never raises; when Redis
    is unavailable the answer is permissive (``ok=True``) so the desk keeps working.
    """
    try:
        actor_id = _operator_id(operator)
        if actor_id is None:
            logger.warning("handle_typing_event: operator without id, ignoring %s", action)
            return {"ok": False, "locked_by": None, "chat_id": int(chat_id)}

        normalized = str(action or "").strip().lower()
        if normalized not in _ACTIONS:
            logger.debug("handle_typing_event: unknown action %r, treated as stop", action)
            normalized = "stop"

        if normalized == "start":
            return await _start(operator, actor_id, int(chat_id))
        return await _stop(actor_id, int(chat_id))
    except Exception as exc:  # noqa: BLE001 - a lock bug must never 500 the request
        logger.exception("handle_typing_event failed (%s: %s)", type(exc).__name__, exc)
        return {"ok": True, "locked_by": None, "chat_id": int(chat_id)}


async def _start(operator: Any, operator_id: int, chat_id: int) -> dict[str, Any]:
    """Acquire (or refresh) the lock and broadcast the resulting owner."""
    display_name = _display_name(operator)
    until = iso_z(utcnow() + timedelta(seconds=TYPING_LOCK_TTL_SECONDS))

    existing = await get_typing_lock(chat_id)
    if existing is not None and int(existing) != operator_id:
        holder_name = await operator_display_name(int(existing))
        logger.info(
            "TYPING_LOCK collision on chat %s: operator %s blocked by %s",
            chat_id,
            operator_id,
            existing,
        )
        await _broadcast(
            EVT_TYPING_LOCK,
            {
                "chat_id": chat_id,
                "operator_id": int(existing),
                "operator_name": holder_name or f"Operator #{int(existing)}",
                "until": until,
            },
        )
        return {"ok": False, "locked_by": holder_name, "chat_id": chat_id}

    acquired = await set_typing_lock(chat_id, operator_id, ttl=TYPING_LOCK_TTL_SECONDS)
    if not acquired:
        # Either Redis is down (permissive) or another operator won the race between GET and SET.
        winner = await get_typing_lock(chat_id)
        if winner is not None and int(winner) != operator_id:
            holder_name = await operator_display_name(int(winner))
            await _broadcast(
                EVT_TYPING_LOCK,
                {
                    "chat_id": chat_id,
                    "operator_id": int(winner),
                    "operator_name": holder_name or f"Operator #{int(winner)}",
                    "until": until,
                },
            )
            return {"ok": False, "locked_by": holder_name, "chat_id": chat_id}
        logger.warning(
            "set_typing_lock(%s) unavailable (Redis down?); allowing operator %s to type",
            chat_id,
            operator_id,
        )
        return {"ok": True, "locked_by": None, "chat_id": chat_id}

    await _broadcast(
        EVT_TYPING_LOCK,
        {
            "chat_id": chat_id,
            "operator_id": operator_id,
            "operator_name": display_name,
            "until": until,
        },
    )
    return {"ok": True, "locked_by": None, "chat_id": chat_id}


async def _stop(operator_id: int, chat_id: int) -> dict[str, Any]:
    """Release the lock (owner only) and broadcast the unlock."""
    released = await clear_typing_lock(chat_id, operator_id)
    if not released:
        logger.debug(
            "TYPING_UNLOCK on chat %s by operator %s: lock absent or held by someone else",
            chat_id,
            operator_id,
        )
    await _broadcast(EVT_TYPING_UNLOCK, {"chat_id": chat_id, "operator_id": operator_id})
    return {"ok": True, "locked_by": None, "chat_id": chat_id}


async def release_chat_lock(chat_id: int, operator_id: int | None = None) -> bool:
    """Force-release the typing lock for ``chat_id`` (used after a successful send).

    Passing ``operator_id`` keeps the compare-and-delete semantics (only the owner can release);
    ``None`` clears it unconditionally (WS disconnect cleanup). A failed release is swallowed:
    the 15 s TTL is the ultimate guarantee. Broadcasts ``TYPING_UNLOCK`` when something changed.
    """
    try:
        released = await clear_typing_lock(int(chat_id), operator_id)
        if released:
            await _broadcast(
                EVT_TYPING_UNLOCK,
                {"chat_id": int(chat_id), "operator_id": operator_id},
            )
        return bool(released)
    except Exception as exc:  # noqa: BLE001 - TTL is the safety net
        logger.warning("release_chat_lock(%s) failed: %s: %s", chat_id, type(exc).__name__, exc)
        return False


async def release_all_locks_for_operator(operator_id: int, chat_ids: list[int] | None = None) -> int:
    """Release every lock held by ``operator_id`` (WS disconnect path).

    The per-chat keys carry no operator index, so callers that know the affected chats pass
    ``chat_ids``. Without a list nothing can be released safely and ``0`` is returned — the
    15 s TTL covers that case.
    """
    if not chat_ids:
        logger.debug(
            "release_all_locks_for_operator(%s): no chat ids given, relying on the TTL",
            operator_id,
        )
        return 0
    released = 0
    for chat_id in {int(value) for value in chat_ids}:
        try:
            current = await get_typing_lock(chat_id)
        except Exception:  # noqa: BLE001
            continue
        if current is None or int(current) != int(operator_id):
            continue
        if await release_chat_lock(chat_id, int(operator_id)):
            released += 1
    return released


def describe_lock(lock: dict[str, Any] | None) -> str:
    """Human label for a lock dict, used by the 409 body of ``POST .../messages``.

    Returns ``""`` for a free chat so callers can branch on truthiness.
    """
    if not lock:
        return ""
    name = lock.get("operator_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    operator_id = lock.get("operator_id")
    return f"Operator #{operator_id}" if operator_id is not None else "another operator"

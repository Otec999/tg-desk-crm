"""Async Redis facade: pub/sub bus between backend and the Telethon worker + typing locks.

Every public coroutine here is **degradation-safe**: if Redis is unreachable it logs a warning
and returns ``None``/``False`` instead of raising. The API must always boot and serve
``/health`` even with Redis down (``/health`` will simply report ``redis: false``).

Channel names and payloads are frozen by CONTRACTS.md §5.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- channel constants
CH_TG_INCOMING: Final[str] = "tg_incoming"
CH_TG_DELETED: Final[str] = "tg_deleted"
CH_TG_EDITS: Final[str] = "tg_edits"
CH_TG_OUTGOING: Final[str] = "tg_outgoing"
CH_TG_OUTGOING_ACK: Final[str] = "tg_outgoing_ack"
#: CONTRACTS_V2 §4 — reaction sync from Telegram (worker -> backend).
CH_TG_REACTIONS: Final[str] = "tg_reactions"
#: CONTRACTS_V3 §1.3 — relay a message into another chat as our own (backend -> worker).
#: Deliberately NOT in ``WORKER_CHANNELS``: that tuple lists channels the backend
#: *consumes*, while this one is published to the worker.
CH_TG_FORWARD: Final[str] = "tg_forward"
#: CONTRACTS_V4 §2 — mirror an operator's CRM reaction onto the Telegram message
#: (backend -> worker). Consumed by the worker, so it stays out of ``WORKER_CHANNELS``
#: for the same reason as ``CH_TG_FORWARD``.
CH_TG_REACTION_OUT: Final[str] = "tg_reaction_out"
#: CONTRACTS_V5 §1.4 — delete a message in Telegram because an operator removed it
#: from the CRM (backend -> worker). Like ``CH_TG_FORWARD``/``CH_TG_REACTION_OUT`` it is
#: *published* to the worker, so it must stay out of ``WORKER_CHANNELS`` (§8 invariant 10).
CH_TG_DELETE_OUT: Final[str] = "tg_delete_out"
CH_WS_FANOUT: Final[str] = "ws_fanout"

# Channels the backend consumes (worker -> backend).
WORKER_CHANNELS: Final[tuple[str, ...]] = (
    CH_TG_INCOMING,
    CH_TG_DELETED,
    CH_TG_EDITS,
    CH_TG_OUTGOING_ACK,
    CH_TG_REACTIONS,
)

TYPING_LOCK_PREFIX: Final[str] = "typing_lock"
TYPING_LOCK_DEFAULT_TTL: Final[int] = 15

_client: Redis | None = None
_client_lock: asyncio.Lock = asyncio.Lock()


# ------------------------------------------------------------------------------- client access
def _build_client() -> Redis:
    """Create a lazily-connecting asyncio Redis client (no I/O happens here)."""
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3.0,
        socket_timeout=5.0,
        health_check_interval=30,
        retry_on_timeout=True,
    )


async def get_redis() -> Redis | None:
    """Return the shared client (creating it on first use) after confirming the server answers."""
    global _client
    async with _client_lock:
        if _client is None:
            _client = _build_client()
    try:
        await _client.ping()
        return _client
    except Exception as exc:  # noqa: BLE001 - Redis being down must never raise into callers
        logger.warning("Redis unavailable (%s): %s", settings.redis_url_safe, exc)
        return None


async def close_redis() -> None:
    """Close the shared client (shutdown path). Never raises."""
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001 - shutdown must never raise
        logger.warning("Redis close failed: %s: %s", type(exc).__name__, exc)


async def ping_redis() -> bool:
    """Health probe for ``/health``. Returns ``False`` when Redis is down."""
    client = await get_redis()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis ping failed: %s: %s", type(exc).__name__, exc)
        return False


# ------------------------------------------------------------------------------------- publish
def encode_payload(payload: dict[str, Any]) -> str:
    """orjson-encode a pub/sub payload (shared with tests and workers)."""
    return orjson.dumps(payload, default=str).decode("utf-8")


async def publish(channel: str, payload: dict[str, Any]) -> int:
    """Publish ``payload`` (JSON) to ``channel``. Returns the number of receivers (0 on failure)."""
    client = await get_redis()
    if client is None:
        logger.warning("publish(%s) skipped: Redis unavailable", channel)
        return 0
    try:
        return int(await client.publish(channel, encode_payload(payload)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish(%s) failed: %s: %s", channel, type(exc).__name__, exc)
        return 0


# ------------------------------------------------------------------------------------- pub/sub
class RedisPubSub:
    """Thin wrapper around ``redis.asyncio.client.PubSub`` with JSON decoding."""

    def __init__(self, client: Redis, pubsub: Any) -> None:
        self._client = client
        self._pubsub = pubsub
        self._closed = False

    @property
    def channels(self) -> tuple[str, ...]:
        try:
            return tuple(self._pubsub.channels.keys())
        except Exception:  # noqa: BLE001 - introspection is best-effort
            return ()

    async def subscribe(self, *channels: str) -> None:
        """Subscribe to ``channels`` (idempotent; duplicates ignored)."""
        wanted = [channel for channel in channels if channel]
        if not wanted:
            raise ValueError("subscribe() requires at least one channel")
        await self._pubsub.subscribe(*wanted)
        logger.info("Redis pubsub subscribed: %s", ", ".join(wanted))

    async def listen(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(channel, data)`` for every message; malformed payloads are logged and skipped.

        ``asyncio.CancelledError`` is always re-raised so task cancellation works; transport
        errors propagate to the caller (``background.redis_consumer`` owns reconnection).
        """
        while not self._closed:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if not message:
                await asyncio.sleep(0)
                continue
            if message.get("type") != "message":
                continue
            channel = message.get("channel")
            raw = message.get("data")
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8", errors="replace")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str):
                logger.warning("Dropping non-text payload on %s: %r", channel, raw)
                continue
            try:
                data = orjson.loads(raw)
            except (orjson.JSONDecodeError, ValueError):
                logger.warning("Dropping malformed JSON on %s: %.200s", channel, raw)
                continue
            if not isinstance(data, dict):
                logger.warning("Dropping non-object payload on %s: %.200s", channel, raw)
                continue
            yield str(channel), data

    async def close(self) -> None:
        """Unsubscribe and release the pub/sub connection. Never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._pubsub.unsubscribe()
        except Exception as exc:  # noqa: BLE001
            logger.debug("pubsub.unsubscribe failed: %s", exc)
        try:
            await self._pubsub.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pubsub.aclose failed: %s: %s", type(exc).__name__, exc)


async def pubsub() -> RedisPubSub | None:
    """Create a dedicated pub/sub connection. Returns ``None`` when Redis is unavailable."""
    client = await get_redis()
    if client is None:
        return None
    try:
        ps = client.pubsub(ignore_subscribe_messages=True)
        wrapper = RedisPubSub(client, ps)
        await ps.ping()
        return wrapper
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis pubsub unavailable: %s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------------- typing lock
def typing_lock_key(chat_id: int) -> str:
    """Key used for the per-chat collision lock (CONTRACTS.md §5)."""
    return f"{TYPING_LOCK_PREFIX}:{int(chat_id)}"


async def set_typing_lock(chat_id: int, operator_id: int, ttl: int = TYPING_LOCK_DEFAULT_TTL) -> bool:
    """Acquire/refresh the typing lock for ``chat_id``. Returns ``True`` on success.

    The lock is advisory: if it is already owned by a different operator the call returns
    ``False`` and the caller answers 409 (typist collision). Same-owner refresh always succeeds.
    """
    client = await get_redis()
    if client is None:
        return False
    key = typing_lock_key(chat_id)
    try:
        current = await client.get(key)
        if current is not None and str(current) != str(operator_id):
            return False
        await client.set(key, str(int(operator_id)), ex=max(1, int(ttl)))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_typing_lock(%s) failed: %s: %s", chat_id, type(exc).__name__, exc)
        return False


async def get_typing_lock(chat_id: int) -> int | None:
    """Return the operator id holding the typing lock for ``chat_id`` (``None`` if free/offline)."""
    client = await get_redis()
    if client is None:
        return None
    try:
        value = await client.get(typing_lock_key(chat_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_typing_lock(%s) failed: %s: %s", chat_id, type(exc).__name__, exc)
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Corrupt typing lock value for chat %s: %r", chat_id, value)
        return None


async def clear_typing_lock(chat_id: int, operator_id: int | None = None) -> bool:
    """Release the lock. Only the owner may clear it (safe compare-and-delete: GET then DEL).

    Pass ``operator_id=None`` for a forced clear (e.g. operator disconnect cleanup). Lua is
    intentionally avoided to stay compatible with any redis-py/Redis 7 configuration.
    """
    client = await get_redis()
    if client is None:
        return False
    key = typing_lock_key(chat_id)
    try:
        current = await client.get(key)
        if current is None:
            return False
        if operator_id is not None and str(current) != str(int(operator_id)):
            return False
        # Re-check immediately before deleting to shrink the (still possible) TOCTOU window.
        again = await client.get(key)
        if again is None or str(again) != str(current):
            return False
        await client.delete(key)
        return True
    except RedisError as exc:
        logger.warning("clear_typing_lock(%s) failed: %s: %s", chat_id, type(exc).__name__, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("clear_typing_lock(%s) failed: %s: %s", chat_id, type(exc).__name__, exc)
        return False


__all__ = [
    "CH_TG_DELETE_OUT",
    "CH_TG_DELETED",
    "CH_TG_EDITS",
    "CH_TG_INCOMING",
    "CH_TG_OUTGOING",
    "CH_TG_OUTGOING_ACK",
    "CH_TG_REACTION_OUT",
    "CH_WS_FANOUT",
    "TYPING_LOCK_DEFAULT_TTL",
    "TYPING_LOCK_PREFIX",
    "WORKER_CHANNELS",
    "RedisPubSub",
    "clear_typing_lock",
    "close_redis",
    "encode_payload",
    "get_redis",
    "get_typing_lock",
    "ping_redis",
    "publish",
    "pubsub",
    "set_typing_lock",
    "typing_lock_key",
]

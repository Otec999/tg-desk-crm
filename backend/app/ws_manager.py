"""WebSocket connection registry and fan-out (CONTRACTS.md §4).

One :class:`ConnectionManager` instance lives for the whole process (``manager``). It owns:

* ``operator_id -> set[WebSocket]`` (one operator may have several tabs open),
* ``operator_id -> last heartbeat monotonic timestamp``,
* serialisation of every server -> client envelope through orjson.

Every operation is protected by a single ``asyncio.Lock`` so concurrent REST handlers,
background tickers and Redis consumers can broadcast safely. Dead sockets are dropped
silently — a broken client must never break a broadcast for the others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, Final

import orjson
from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Canonical server -> client event types (CONTRACTS.md §4). Kept as constants so no module
# ever typos an event name again.
EVT_CONNECTED: Final[str] = "CONNECTED"
EVT_NEW_MESSAGE: Final[str] = "NEW_MESSAGE"
EVT_MESSAGE_DELETED: Final[str] = "MESSAGE_DELETED"
EVT_SLA_UPDATE: Final[str] = "SLA_UPDATE"
EVT_SLA_RESOLVED: Final[str] = "SLA_RESOLVED"
EVT_TYPING_LOCK: Final[str] = "TYPING_LOCK"
EVT_TYPING_UNLOCK: Final[str] = "TYPING_UNLOCK"
EVT_SHIFT_CHANGED: Final[str] = "SHIFT_CHANGED"
EVT_OPERATOR_PRESENCE: Final[str] = "OPERATOR_PRESENCE"
EVT_CHAT_ASSIGNED: Final[str] = "CHAT_ASSIGNED"
EVT_MESSAGE_STATUS: Final[str] = "MESSAGE_STATUS"
EVT_ERROR: Final[str] = "ERROR"
EVT_INTERNAL_ERROR: Final[str] = "INTERNAL_ERROR"
EVT_PONG: Final[str] = "PONG"
EVT_SUBSCRIBED: Final[str] = "SUBSCRIBED"

WS_CLOSE_TOKEN_INVALID: Final[int] = 1008  # policy violation
WS_CLOSE_RESTART: Final[int] = 1012  # service restart


def utcnow_iso() -> str:
    """Current UTC time as ``2024-06-01T12:00:00Z`` (contract datetime format)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the only shape the server may ever send: ``{"type","ts","data"}``."""
    return {
        "type": str(event_type),
        "ts": utcnow_iso(),
        "data": data if isinstance(data, dict) else {},
    }


def encode_envelope(payload: dict[str, Any]) -> str:
    """orjson-serialise an envelope, falling back to ``default=str`` for stray datetimes."""
    return orjson.dumps(payload, default=str).decode("utf-8")


class ConnectionManager:
    """Tracks live WebSocket connections and heartbeats per operator."""

    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}
        self._heartbeats: dict[int, float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Kept outside the lock: monotonic reads are safe without it.
        self._started_at: float = time.monotonic()

    # ------------------------------------------------------------------ connection lifecycle
    async def connect(self, ws: WebSocket, operator: Any) -> int:
        """Register ``ws`` for ``operator`` (an ``Operator`` row: needs ``.id``).

        The caller is responsible for ``ws.accept()`` — the manager never accepts, so the
        endpoint can decide the accept/close ordering (see ``main.websocket_endpoint``).
        """
        operator_id = int(getattr(operator, "id"))
        async with self._lock:
            self._connections.setdefault(operator_id, set()).add(ws)
            self._heartbeats[operator_id] = time.monotonic()
            total = sum(len(sockets) for sockets in self._connections.values())
        logger.info(
            "WS connected operator_id=%s username=%s (sockets=%d, operators=%d)",
            operator_id,
            getattr(operator, "username", "?"),
            total,
            len(self._connections),
        )
        return operator_id

    async def disconnect(self, ws: WebSocket, operator_id: int) -> bool:
        """Unregister ``ws``. Returns ``True`` when the operator has no sockets left."""
        async with self._lock:
            sockets = self._connections.get(operator_id)
            if sockets is not None:
                sockets.discard(ws)
                if not sockets:
                    self._connections.pop(operator_id, None)
                    operator_gone = True
                else:
                    operator_gone = False
            else:
                operator_gone = True
            if operator_gone:
                self._heartbeats.pop(operator_id, None)
            remaining = sum(len(s) for s in self._connections.values())
        logger.info(
            "WS disconnected operator_id=%s (sockets=%d, operator_fully_offline=%s)",
            operator_id,
            remaining,
            operator_gone,
        )
        return operator_gone

    # --------------------------------------------------------------------------- heartbeats
    def touch_heartbeat(self, operator_id: int) -> None:
        """Record client activity for ``operator_id`` (called on HEARTBEAT and any frame)."""
        self._heartbeats[int(operator_id)] = time.monotonic()

    def last_heartbeat_age(self, operator_id: int) -> float | None:
        """Seconds since the last heartbeat, or ``None`` when the operator is not tracked."""
        last = self._heartbeats.get(int(operator_id))
        if last is None:
            return None
        return time.monotonic() - last

    async def sweep_stale(self, timeout_seconds: float) -> list[int]:
        """Remove sockets of silent operators; return the ids that fully timed out.

        A timed-out operator is dropped from the registry and from the heartbeat map, and its
        live sockets are closed with 1012. The *database* side effect (``is_online_ws=False``
        and ``ONLINE -> PAUSE`` fallback) is performed by ``background.heartbeat_sweeper``.
        """
        if timeout_seconds <= 0:
            return []
        now = time.monotonic()
        timed_out: list[int] = []
        stale_sockets: list[tuple[int, WebSocket]] = []

        async with self._lock:
            for operator_id in list(self._connections):
                last = self._heartbeats.get(operator_id)
                if last is None:
                    # Tracked socket without a heartbeat entry: start the clock now.
                    self._heartbeats[operator_id] = now
                    continue
                if now - last <= timeout_seconds:
                    continue
                timed_out.append(operator_id)
                for ws in list(self._connections.pop(operator_id, set())):
                    stale_sockets.append((operator_id, ws))
                self._heartbeats.pop(operator_id, None)

        for operator_id, ws in stale_sockets:
            try:
                await ws.close(code=WS_CLOSE_RESTART)
            except Exception as exc:  # noqa: BLE001 - closing a dead socket is best-effort
                logger.debug("stale close failed operator_id=%s: %s", operator_id, exc)

        if timed_out:
            logger.warning("Heartbeat sweep timed out operators: %s", sorted(timed_out))
        return timed_out

    # ------------------------------------------------------------------------------ sending
    @staticmethod
    async def _send_envelope(ws: WebSocket, payload: dict[str, Any] | str) -> bool:
        """Serialise + send one frame. Returns ``False`` when the socket is unusable."""
        text = payload if isinstance(payload, str) else encode_envelope(payload)
        try:
            await ws.send_text(text)
            return True
        except Exception as exc:  # noqa: BLE001 - dead socket: drop it, never propagate
            logger.debug("send_text failed, dropping socket: %s: %s", type(exc).__name__, exc)
            return False

    async def broadcast(
        self,
        envelope_payload: dict[str, Any] | str,
        exclude_operator_id: int | None = None,
    ) -> int:
        """Send ``envelope_payload`` to every connected socket (optionally excluding one operator).

        ``ts`` is added automatically when the payload is a dict and has no ``ts`` yet.
        Returns the number of sockets the frame was delivered to. Dead sockets are removed
        silently and do not raise.
        """
        if isinstance(envelope_payload, dict) and "ts" not in envelope_payload:
            envelope_payload = {**envelope_payload, "ts": utcnow_iso()}

        async with self._lock:
            targets: list[tuple[int, WebSocket]] = [
                (operator_id, ws)
                for operator_id, sockets in self._connections.items()
                if exclude_operator_id is None or operator_id != exclude_operator_id
                for ws in list(sockets)
            ]

        if not targets:
            return 0

        results = await asyncio.gather(
            *(self._send_envelope(ws, envelope_payload) for _, ws in targets),
        )
        delivered = 0
        dead: list[tuple[int, WebSocket]] = []
        for (operator_id, ws), ok in zip(targets, results, strict=True):
            if ok:
                delivered += 1
            else:
                dead.append((operator_id, ws))

        for operator_id, ws in dead:
            await self.disconnect(ws, operator_id)
        return delivered

    async def send_to_operator(self, operator_id: int, envelope_payload: dict[str, Any] | str) -> int:
        """Send to every socket of one operator. Returns delivered frame count."""
        if isinstance(envelope_payload, dict) and "ts" not in envelope_payload:
            envelope_payload = {**envelope_payload, "ts": utcnow_iso()}

        async with self._lock:
            sockets = list(self._connections.get(operator_id, set()))

        if not sockets:
            return 0

        results = await asyncio.gather(*(self._send_envelope(ws, envelope_payload) for ws in sockets))
        delivered = 0
        for ws, ok in zip(sockets, results, strict=True):
            if ok:
                delivered += 1
            else:
                await self.disconnect(ws, operator_id)
        return delivered

    async def send_error(self, ws: WebSocket, detail: str, event_type: str = EVT_ERROR) -> None:
        """Send an ``ERROR`` (or ``INTERNAL_ERROR``) envelope to one socket, best-effort."""
        await self._send_envelope(ws, envelope(event_type, {"detail": detail}))

    # -------------------------------------------------------------------------- introspection
    def online_operator_ids(self) -> list[int]:
        """Operator ids that currently hold at least one live socket (sorted)."""
        return sorted(operator_id for operator_id, sockets in self._connections.items() if sockets)

    def connection_count(self) -> int:
        """Total number of live sockets across all operators."""
        return sum(len(sockets) for sockets in self._connections.values())

    def operator_connection_count(self, operator_id: int) -> int:
        """Number of sockets held by one operator."""
        return len(self._connections.get(int(operator_id), ()))

    def is_online(self, operator_id: int) -> bool:
        return bool(self._connections.get(int(operator_id)))

    def snapshot(self) -> dict[str, Any]:
        """Lightweight diagnostics for ``/api/system/info``."""
        return {
            "operators_online": len(self.online_operator_ids()),
            "ws_clients": self.connection_count(),
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
        }

    async def close_all(self, code: int = WS_CLOSE_RESTART) -> None:
        """Close every socket (shutdown). Never raises."""
        async with self._lock:
            targets = [ws for sockets in self._connections.values() for ws in sockets]
            self._connections.clear()
            self._heartbeats.clear()
        if not targets:
            return
        logger.info("Closing %d WebSocket connection(s) with code %s", len(targets), code)
        await asyncio.gather(
            *(self._safe_close(ws, code) for ws in targets),
            return_exceptions=True,
        )

    @staticmethod
    async def _safe_close(ws: WebSocket, code: int) -> None:
        try:
            await ws.close(code=code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("close failed: %s: %s", type(exc).__name__, exc)


manager: ConnectionManager = ConnectionManager()

__all__ = [
    "EVT_CHAT_ASSIGNED",
    "EVT_CONNECTED",
    "EVT_ERROR",
    "EVT_MESSAGE_DELETED",
    "EVT_MESSAGE_STATUS",
    "EVT_NEW_MESSAGE",
    "EVT_OPERATOR_PRESENCE",
    "EVT_PONG",
    "EVT_SHIFT_CHANGED",
    "EVT_SLA_RESOLVED",
    "EVT_SLA_UPDATE",
    "EVT_TYPING_LOCK",
    "EVT_TYPING_UNLOCK",
    "WS_CLOSE_RESTART",
    "WS_CLOSE_TOKEN_INVALID",
    "ConnectionManager",
    "encode_envelope",
    "envelope",
    "manager",
    "utcnow_iso",
]

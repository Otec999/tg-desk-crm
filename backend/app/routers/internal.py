"""Internal ops router (§3.7): supervisor broadcast + system status card.

``GET /system/info`` is deliberately read-only and resilient: when Redis is
unreachable the card reports ``telegram_connected=False`` and an empty worker map
instead of failing, because the top bar polls it continuously.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers._common import as_contract, safe_broadcast
from app.schemas import BroadcastOut, BroadcastRequest, SystemInfoOut
from app.security import get_current_operator, require_supervisor

logger = logging.getLogger("app.routers.internal")

router = APIRouter(tags=["internal"])

#: TTL-based liveness key written by the Telethon worker (CONTRACTS.md §3.7).
WORKER_HEARTBEAT_KEY: Final[str] = "worker:heartbeat"
#: Hash describing every connected worker: ``worker_id -> status JSON``.
WORKER_STATUS_KEY: Final[str] = "worker:status"
#: Upper bound for both Redis probes - the top bar must never hang on them.
REDIS_PROBE_TIMEOUT_S: Final[float] = 1.5
#: Number of live background tasks that counts as "the SLA engine is ticking".
SLA_TICKER_TASK: Final[str] = "sla_ticker"


async def _probe_redis() -> tuple[bool, dict[str, Any]]:
    """Return ``(worker_heartbeat_present, worker_status_mapping)``.

    Both keys are read in one ``gather`` under a hard timeout. Any failure -
    missing key, Redis down, slow server - degrades to ``False`` / ``{}``; the
    worker is simply reported as disconnected.
    """
    try:
        from app.redis_bus import get_redis

        client = await get_redis()
        if client is None:
            logger.debug("system/info: Redis unavailable, reporting worker offline")
            return False, {}
        exists, raw_status = await asyncio.wait_for(
            asyncio.gather(
                client.exists(WORKER_HEARTBEAT_KEY),
                client.hgetall(WORKER_STATUS_KEY),
            ),
            timeout=REDIS_PROBE_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("system/info: redis probe timed out after %.1fs", REDIS_PROBE_TIMEOUT_S)
        return False, {}
    except Exception as exc:  # noqa: BLE001 - Redis down is a normal degraded state
        logger.warning("system/info: redis probe failed: %s: %s", type(exc).__name__, exc)
        return False, {}

    workers: dict[str, Any] = {}
    if isinstance(raw_status, dict):
        workers = {str(key): value for key, value in raw_status.items()}
    return bool(exists), workers


def _ws_clients() -> int:
    """Current number of connected WebSocket clients (``0`` when unavailable)."""
    try:
        from app.ws_manager import manager

        return int(manager.connection_count())
    except Exception as exc:  # noqa: BLE001 - the WS manager is optional at boot
        logger.debug("connection_count unavailable: %s", exc)
        return 0


def _sla_ticking(request: Request | None = None) -> bool:
    """``True`` while the background ``sla_ticker`` task is alive.

    ``app.background.start_all`` stores one task per worker in
    ``app.state.tasks``; when the request carries the app we can verify the real
    state, otherwise the ticker is assumed to be running (it is started by the
    lifespan and cannot be paused through the API).
    """
    app = getattr(getattr(request, "app", None), "state", None)
    tasks = getattr(app, "tasks", None)
    if isinstance(tasks, dict) and tasks:
        matching = [task for name, task in tasks.items() if name == SLA_TICKER_TASK]
        if matching:
            return any(not bool(getattr(task, "done", lambda: True)()) for task in matching)
        return any(not bool(getattr(task, "done", lambda: True)()) for task in tasks.values())
    return True


@router.post(
    "/internal/broadcast",
    response_model=BroadcastOut,
    status_code=status.HTTP_200_OK,
    summary="Broadcast a raw WS envelope (supervisor only)",
    responses={
        403: {"description": "Supervisor privileges required"},
        422: {"description": "Malformed envelope"},
    },
)
async def internal_broadcast(
    payload: BroadcastRequest,
    operator: Annotated[Any, Depends(require_supervisor)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BroadcastOut:
    """Fan a validated ``{type, data}`` envelope out to every connected operator."""
    event = payload.event
    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="event must be a WS envelope object with 'type' and 'data'",
        )
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="event.type is required and must be a non-empty string",
        )
    if "data" not in event:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="event.data is required",
        )
    data = event.get("data")
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="event.data must be an object",
        )

    # The envelope is forwarded as-is (§3.7); safe_broadcast stamps ``ts``.
    delivered = await safe_broadcast(event_type.strip(), data)
    logger.info(
        "supervisor %s broadcast %s to %s (delivered=%s)",
        getattr(operator, "id", None),
        event_type.strip(),
        getattr(payload, "target", "ALL"),
        delivered,
    )
    return BroadcastOut.model_validate({"ok": True})


@router.get(
    "/system/info",
    response_model=SystemInfoOut,
    status_code=status.HTTP_200_OK,
    summary="Operational status card (worker heartbeat, WS clients, SLA ticker)",
)
async def system_info(
    request: Request,
    operator: Annotated[Any, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemInfoOut:
    """Return ``SystemInfoOut`` describing the live runtime state."""
    telegram_connected, workers = await _probe_redis()
    payload = {
        "telegram_connected": telegram_connected,
        "ws_clients": _ws_clients(),
        "workers": workers,
        "sla_ticking": _sla_ticking(request),
    }
    logger.debug("system info requested by operator %s: %s", getattr(operator, "id", None), payload)
    return as_contract("SystemInfoOut", payload)

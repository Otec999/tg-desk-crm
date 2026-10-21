"""TG-Desk CRM backend entrypoint.

Run with::

    cd backend
    python -m uvicorn app.main:app --reload --port 8000

or ``python backend/app/main.py`` for the plain uvicorn runner.

Startup is deliberately forgiving: PostgreSQL and Redis may be down and the API still serves
``/health`` with ``status: degraded``. Routers written by other agents are mounted behind
per-router import guards so one broken module never prevents the process from booting.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app import background
from app.config import settings
from app.database import dispose_engine, get_db, init_models, ping_db
from app.redis_bus import (
    CH_WS_FANOUT,
    TYPING_LOCK_DEFAULT_TTL,
    clear_typing_lock,
    close_redis,
    get_redis,
    ping_redis,
    publish,
    set_typing_lock,
)
from app.utils import iso_z, utcnow
from app.ws_manager import (
    EVT_CONNECTED,
    EVT_INTERNAL_ERROR,
    EVT_OPERATOR_PRESENCE,
    EVT_PONG,
    EVT_SUBSCRIBED,
    EVT_TYPING_LOCK,
    EVT_TYPING_UNLOCK,
    WS_CLOSE_TOKEN_INVALID,
    encode_envelope,
    envelope,
    manager,
)

APP_NAME = "TG-Desk CRM API"
APP_VERSION = "1.0.0"
API_PREFIX = "/api"

logger = logging.getLogger(__name__)

#: router module -> URL prefix relative to /api. The table is empty on purpose: every router the
#: API agent writes already declares its own ``APIRouter(prefix="/auth")`` etc., so mounting with
#: a second prefix would produce duplicated paths such as ``/api/auth/auth/login``.
ROUTER_MOUNTS: tuple[str, ...] = (
    "auth",
    "chats",
    "messages",
    "shift",
    "sla",
    "tx",
    "internal",
    "media",
    "reactions",
    "quick_replies",
    "admin",
    "search",
)
#: Every router declares bare paths (``/admin/...``, ``/media/...``) and is mounted under
#: the shared ``/api`` prefix, so the public surface is ``/api/admin/...`` etc.
ROOT_MOUNTS: tuple[str, ...] = ()


# =============================================================================== configuration
def configure_logging() -> None:
    """Configure ``logging.basicConfig`` exactly once for the whole process."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def _log_config() -> None:
    """Log the effective (secret-free) configuration."""
    logger.info("Starting %s v%s", APP_NAME, APP_VERSION)
    logger.info("app_env=%s", settings.app_env)
    logger.info("database: %s", settings.database_url_safe)
    logger.info("redis: %s", settings.redis_url_safe)
    logger.info("cors origins: %s", ", ".join(settings.cors_origins) or "<none>")
    logger.info(
        "sla_seconds=%s ws_heartbeat_timeout=%ss ws_tick=%ss",
        settings.backend_sla_seconds,
        settings.backend_ws_heartbeat_timeout_seconds,
        settings.backend_ws_tick_seconds,
    )


def enum_value(value: Any) -> str:
    """String value of an enum member / plain string (``ShiftStatus.ONLINE`` -> ``"ONLINE"``)."""
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else str(raw)


# =================================================================================== lifespan
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot: logging, schema, Redis, background tasks. Shutdown: tasks, Redis, engine."""
    configure_logging()
    _log_config()

    try:
        await init_models()
        logger.info("Database schema ready")
    except Exception as exc:  # noqa: BLE001 - degraded start beats a crash loop
        logger.warning(
            "Database unreachable at startup (%s: %s). Serving /health with status=degraded.",
            type(exc).__name__,
            exc,
        )

    if await get_redis() is None:
        logger.warning("Redis unreachable at startup; pub/sub and typing locks degrade gracefully.")

    await background.start_all(app)
    logger.info("Startup complete")

    try:
        yield
    finally:
        logger.info("Shutting down %s", APP_NAME)
        await manager.close_all()
        await background.stop_all(app)
        await close_redis()
        await dispose_engine()
        logger.info("Shutdown complete")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Telegram-based merchant support desk with SLA tracking (CONTRACTS.md).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ================================================================================== routers
def _extra_router_attrs(module: Any) -> list[str]:
    """Names of secondary ``APIRouter`` objects declared by a router module.

    Matches the ``<something>_router`` convention and skips the module's primary
    ``router``. Ordering is alphabetical so mounting is deterministic.
    """
    from fastapi import APIRouter  # noqa: PLC0415 - avoids a circular import at module load

    names: list[str] = []
    for attr in dir(module):
        if attr == "router" or not attr.endswith("_router"):
            continue
        if attr.startswith("_"):
            continue
        if isinstance(getattr(module, attr, None), APIRouter):
            names.append(attr)
    return sorted(names)


def mount_router(module_name: str, prefix: str = API_PREFIX) -> bool:
    """Import ``app.routers.<module_name>`` and mount its ``router`` under ``prefix`` (/api).

    A missing or broken router is logged loudly but never blocks the boot. The router modules
    already declare their own sub-prefix (``/auth``, ``/chats``, ...), so only ``/api`` is added
    here; anything else would duplicate segments (``/api/auth/auth/login``).
    """
    import importlib

    try:
        module = importlib.import_module(f"app.routers.{module_name}")
    except ImportError as exc:
        logger.error("Router '%s' unavailable, skipping: %s", module_name, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - syntax error / failure inside the module
        logger.exception("Router '%s' failed to import (%s), skipping", module_name, type(exc).__name__)
        return False

    router = getattr(module, "router", None)
    if router is None:
        logger.error("Router module '%s' exposes no `router`, skipping", module_name)
        return False

    try:
        app.include_router(router, prefix=prefix)
    except Exception as exc:  # noqa: BLE001 - duplicate operation ids and similar
        logger.exception("Router '%s' failed to mount (%s), skipping", module_name, type(exc).__name__)
        return False

    # A module may expose extra top-level routers whose paths are NOT relative to the
    # module's own prefix — e.g. ``app.routers.messages.message_router`` serves
    # ``/messages/{id}`` while ``router`` serves ``/chats``. They are discovered by
    # convention (``<name>_router``), so CONTRACTS_V3 §2.3 routes land on
    # ``/api/messages/...`` instead of the nested ``/api/chats/messages/...``.
    for attr in _extra_router_attrs(module):
        extra = getattr(module, attr)
        try:
            app.include_router(extra, prefix=API_PREFIX)
        except Exception as exc:  # noqa: BLE001 - never block the boot
            logger.exception(
                "Router '%s.%s' failed to mount (%s), skipping", module_name, attr, type(exc).__name__
            )

    logger.info("Router mounted: %s (%s)", module_name, ", ".join(sorted({r.path for r in router.routes})))
    return True


MOUNTED_ROUTERS: dict[str, bool] = {
    module_name: mount_router(module_name) for module_name in ROUTER_MOUNTS
}
# V2 routers own their absolute paths, so they are mounted without the ``/api`` prefix.
MOUNTED_ROUTERS.update(
    {module_name: mount_router(module_name, prefix="") for module_name in ROOT_MOUNTS}
)


# ================================================================================== handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 responses keep the documented ``{"detail": ...}`` envelope.

    pydantic v2 stores the raised exception object in ``ctx["error"]`` for
    ``value_error`` entries, which plain ``json.dumps`` cannot serialise — that
    used to turn every domain-level validation failure into a 500. The errors are
    therefore reduced to their JSON-safe parts (``type``/``loc``/``msg``/``input``)
    and ``ctx``/``url`` are dropped so the payload is stable across pydantic
    releases and actually serialisable.
    """
    safe_errors: list[dict[str, Any]] = []
    for error in exc.errors():
        entry: dict[str, Any] = {
            "type": str(error.get("type", "value_error")),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "Invalid value")),
        }
        if "input" in error and isinstance(error["input"], (str, int, float, bool, type(None))):
            entry["input"] = error["input"]
        safe_errors.append(entry)
    logger.info("Validation error on %s %s: %s", request.method, request.url.path, safe_errors)
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all 500: logged with traceback, never leaking internals to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ==================================================================================== health
@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    """Liveness/readiness probe: reports dependency status, never raises."""
    db_ok = await ping_db()
    redis_ok = await ping_redis()
    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "app_env": settings.app_env,
        "version": APP_VERSION,
    }


# ================================================================================= WebSocket
async def load_operator_from_token(token: str) -> Any:
    """Decode the JWT and load the operator row; raises on any failure.

    Depends on (security agent): ``decode_access_token(token) -> TokenPayload`` — a validated
    claims object exposing ``.id`` — raising ``InvalidTokenError`` on any failure. A bare int
    return is also tolerated so either implementation shape keeps working.
    """
    from app import models as models_module  # lazy: models agent owns this module
    from app.security import decode_access_token  # lazy: security agent owns this module

    claims: Any = decode_access_token(token)
    operator_id = int(claims.id if hasattr(claims, "id") else claims)

    async for session in get_db():
        operator = await session.get(getattr(models_module, "Operator"), operator_id)
        if operator is None:
            raise ValueError(f"operator {operator_id} not found")
        return operator
    raise ValueError("database session unavailable")


def operator_out_payload(operator: Any) -> dict[str, Any]:
    """Serialise ``OperatorOut`` via the schemas agent's model, with a minimal fallback."""
    try:
        from app import schemas as schemas_module  # lazy

        payload = (
            getattr(schemas_module, "OperatorOut")
            .model_validate(operator, from_attributes=True)
            .model_dump(mode="json")
        )
        if isinstance(payload, dict) and payload:
            return payload
    except Exception as exc:  # noqa: BLE001 - partial checkout / schema not written yet
        logger.debug("OperatorOut unavailable (%s); using minimal payload", type(exc).__name__)

    return {
        "id": int(getattr(operator, "id")),
        "username": str(getattr(operator, "username", "")),
        "display_name": str(getattr(operator, "display_name", "")),
        "role": enum_value(getattr(operator, "role", "OPERATOR")),
        "shift_status": enum_value(getattr(operator, "shift_status", "OFFLINE")),
        "is_online_ws": bool(getattr(operator, "is_online_ws", False)),
        "last_seen_at": None,
        "sla_target_seconds": int(getattr(operator, "sla_target_seconds", settings.backend_sla_seconds)),
        "created_at": None,
    }


async def set_presence(operator_id: int, is_online_ws: bool) -> None:
    """Persist socket presence; a DB outage must not break the socket loop."""
    try:
        from app import models as models_module  # lazy

        async for session in get_db():
            operator = await session.get(getattr(models_module, "Operator"), int(operator_id))
            if operator is None:
                return
            operator.is_online_ws = is_online_ws
            if is_online_ws:
                operator.last_seen_at = datetime.now(UTC)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("presence update failed operator_id=%s: %s: %s", operator_id, type(exc).__name__, exc)


async def broadcast_presence(operator: Any, is_online_ws: bool) -> None:
    """Tell every client that ``operator``'s socket presence changed."""
    await manager.broadcast(
        envelope(
            EVT_OPERATOR_PRESENCE,
            {
                "operator_id": int(getattr(operator, "id")),
                "is_online_ws": is_online_ws,
                "shift_status": enum_value(getattr(operator, "shift_status", "OFFLINE")),
            },
        )
    )


async def handle_typing(websocket: WebSocket, operator: Any, raw_chat_id: Any, message_type: str) -> None:
    """Delegate TYPING_LOCK / TYPING_UNLOCK to the typing service (Redis fallback if absent).

    Documented signature (services agent):
        async def handle_typing_event(operator, chat_id: int, action: str) -> dict | None
    """
    if raw_chat_id is None:
        await manager.send_error(websocket, "chat_id required")
        return
    try:
        chat_id = int(raw_chat_id)
    except (TypeError, ValueError):
        await manager.send_error(websocket, "chat_id must be an integer")
        return

    operator_id = int(getattr(operator, "id"))
    display_name = str(getattr(operator, "display_name", ""))
    action = "start" if message_type == EVT_TYPING_LOCK else "stop"
    payload: dict[str, Any]
    # ``until`` must be the lock DEADLINE, never "now": the client renders a countdown
    # from it and treats a past value as an already-expired lock.
    until = iso_z(utcnow() + timedelta(seconds=TYPING_LOCK_DEFAULT_TTL))

    try:
        from app.services.typing import handle_typing_event  # lazy: services agent owns this module

        result = await handle_typing_event(operator, chat_id, action)
        payload = result if isinstance(result, dict) else {"ok": True, "locked_by": None}
        if action == "start":
            # ``handle_typing_event`` already broadcast the authoritative TYPING_LOCK
            # (including the holder's real name on a collision). Broadcasting a second
            # frame here would re-assert THIS operator as the owner and overwrite the
            # true holder on every client — the bug that made inputs untypeable.
            if payload.get("ok"):
                logger.debug("typing lock acquired on chat %s by operator %s", chat_id, operator_id)
        else:
            await manager.broadcast(
                envelope(EVT_TYPING_UNLOCK, {"chat_id": chat_id, "operator_id": operator_id})
            )
    except ImportError as exc:
        logger.warning("typing service unavailable (%s); using the redis lock fallback", exc)
        if action == "start":
            acquired = await set_typing_lock(chat_id, operator_id)
            payload = {"ok": bool(acquired), "locked_by": display_name if acquired else None}
            if acquired:
                await manager.broadcast(
                    envelope(
                        EVT_TYPING_LOCK,
                        {
                            "chat_id": chat_id,
                            "operator_id": operator_id,
                            "operator_name": display_name,
                            "until": until,
                        },
                    )
                )
        else:
            await clear_typing_lock(chat_id, operator_id)
            payload = {"ok": True, "locked_by": None}
            await manager.broadcast(
                envelope(EVT_TYPING_UNLOCK, {"chat_id": chat_id, "operator_id": operator_id})
            )
    except Exception as exc:  # noqa: BLE001 - a service bug must not kill the socket
        logger.exception("typing handler failed (%s)", type(exc).__name__)
        await manager.send_error(websocket, "typing handler failed")
        return

    # Command acknowledgement for the ORIGINATING socket only. It must not reuse the
    # event type: a TYPING_LOCK frame without ``operator_id`` would be read by the client
    # as "somebody else holds this chat" and lock the very operator who is typing.
    await websocket.send_text(
        encode_envelope(envelope("TYPING_ACK", {**payload, "chat_id": chat_id}))
    )

    try:
        await publish(CH_WS_FANOUT, envelope(message_type, {"chat_id": chat_id, "operator_id": operator_id}))
    except Exception as exc:  # noqa: BLE001 - Redis fan-out is optional
        logger.debug("typing fan-out publish skipped: %s", exc)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None) -> None:
    """Realtime channel (CONTRACTS.md §4).

    Accept-then-close is intentional: browsers only observe the close code once the handshake
    completed, so an invalid token yields a clean ``1008`` rather than an opaque handshake error.
    """
    await websocket.accept()

    if not token:
        logger.info("WS rejected: missing token")
        await websocket.close(code=WS_CLOSE_TOKEN_INVALID)
        return

    try:
        operator = await load_operator_from_token(token)
    except Exception as exc:  # noqa: BLE001 - InvalidTokenError, ValueError, DB failures
        logger.warning("WS auth rejected: %s: %s", type(exc).__name__, exc)
        await websocket.close(code=WS_CLOSE_TOKEN_INVALID)
        return

    operator_id = await manager.connect(websocket, operator)
    await set_presence(operator_id, True)

    try:
        await websocket.send_text(
            encode_envelope(envelope(EVT_CONNECTED, {"operator": operator_out_payload(operator)}))
        )
        await broadcast_presence(operator, True)

        while True:
            try:
                raw = await websocket.receive_json()
            except (ValueError, TypeError) as exc:
                await manager.send_error(websocket, f"expected a JSON object ({type(exc).__name__})")
                continue

            if not isinstance(raw, dict):
                await manager.send_error(websocket, "expected a JSON object")
                continue

            manager.touch_heartbeat(operator_id)
            message_type = str(raw.get("type", "")).upper()

            if message_type == "PING":
                await websocket.send_text(
                    encode_envelope(envelope(EVT_PONG, {"server_time": envelope("_")["ts"]}))
                )
            elif message_type == "HEARTBEAT":
                await set_presence(operator_id, True)
            elif message_type == "SUBSCRIBE":
                # Broadcast model: every client already receives everything. Ack only.
                raw_chat_id = raw.get("chat_id")
                await websocket.send_text(
                    encode_envelope(
                        envelope(
                            EVT_SUBSCRIBED,
                            {"chat_id": int(raw_chat_id) if raw_chat_id is not None else None},
                        )
                    )
                )
            elif message_type in {EVT_TYPING_LOCK, EVT_TYPING_UNLOCK}:
                await handle_typing(websocket, operator, raw.get("chat_id"), message_type)
            elif message_type:
                logger.debug("WS unknown client message type=%s operator=%s", message_type, operator_id)
            else:
                await manager.send_error(websocket, "missing 'type'")

    except WebSocketDisconnect as exc:
        logger.info("WS client disconnected operator_id=%s code=%s", operator_id, exc.code)
    except Exception as exc:  # noqa: BLE001 - one socket must never take down the app
        logger.exception("WS loop error operator_id=%s (%s)", operator_id, type(exc).__name__)
        await manager.send_error(websocket, "internal error", event_type=EVT_INTERNAL_ERROR)
    finally:
        fully_offline = await manager.disconnect(websocket, operator_id)
        if fully_offline:
            await set_presence(operator_id, False)
            await broadcast_presence(operator, False)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            logger.debug("WS close ignored operator_id=%s", operator_id)


# ============================================================================= direct runner
if __name__ == "__main__":
    import uvicorn

    configure_logging()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")

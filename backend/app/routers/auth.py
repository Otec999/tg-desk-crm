"""Authentication router (§3.1): login, current operator, logout.

Login is protected by a small in-memory failure limiter (10 failed attempts per
username per 5 minutes -> ``429``). It is intentionally process-local: the CRM
runs a single API process in front of one PostgreSQL/Redis pair, and the limiter
is a brute-force speed bump rather than a distributed quota.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Operator
from app.routers._common import operator_out, safe_broadcast
from app.routers._provisioning import ensure_supervisor, sync_supervisor_password
from app.schemas import LoginRequest, LoginResponse, OkOut, OperatorOut
from app.security import create_access_token, get_current_operator, verify_password

logger = logging.getLogger("app.routers.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Naive in-memory login rate limiter
# ---------------------------------------------------------------------------

#: Failed login attempts are remembered for this window (seconds).
RATE_LIMIT_WINDOW_SECONDS = 300.0
#: A username may fail this many times inside the window before being throttled.
RATE_LIMIT_MAX_FAILURES = 10
#: Hard cap on tracked usernames so memory cannot grow without bound.
RATE_LIMIT_MAX_TRACKED = 8192

INVALID_CREDENTIALS = "Invalid username or password"
#: ``429`` is present in every modern Starlette; the literal keeps older pins working.
TOO_MANY_REQUESTS = getattr(status, "HTTP_429_TOO_MANY_REQUESTS", 429)


class FailedLoginLimiter:
    """Sliding-window failure counter, keyed by an arbitrary string.

    Extracted from the module-level functions below so several independent logins
    (the desk form and the admin form) can share one implementation while keeping
    **separate** budgets: an attack on ``/api/auth/login`` must never throttle the
    administrator out of ``/api/admin/login``. The mechanics are unchanged — a
    ``deque`` of monotonic timestamps per key, pruned lazily on access, with the
    oldest quarter of buckets evicted once ``max_tracked`` is reached.

    Not thread-safe (CPython's GIL makes the individual operations atomic, and the
    API runs one event loop per process); this is a brute-force speed bump, not a
    distributed quota.
    """

    def __init__(
        self,
        *,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        max_failures: int = RATE_LIMIT_MAX_FAILURES,
        max_tracked: int = RATE_LIMIT_MAX_TRACKED,
        namespace: str = "",
    ) -> None:
        self.window_seconds = float(window_seconds)
        self.max_failures = int(max_failures)
        self.max_tracked = int(max_tracked)
        #: Prepended to every key so two limiters never share a bucket even if a
        #: caller (or a test) were to reuse the module-level ``_failures`` dict.
        self.namespace = namespace
        self._failures: dict[str, deque[float]] = {}

    # -- internals -------------------------------------------------------
    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}" if self.namespace else key

    def _prune(self, window: deque[float], now: float) -> None:
        """Drop timestamps that fell out of the sliding window."""
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()

    # -- public API ------------------------------------------------------
    def is_limited(self, key: str, now: float) -> bool:
        """Return ``True`` when ``key`` exhausted its failure budget."""
        tracked = self._key(key)
        window = self._failures.get(tracked)
        if window is None:
            return False
        self._prune(window, now)
        if not window:
            self._failures.pop(tracked, None)
            return False
        return len(window) >= self.max_failures

    def record_failure(self, key: str, now: float) -> int:
        """Register one failed attempt for ``key``; return the tracked count."""
        tracked = self._key(key)
        window = self._failures.get(tracked)
        if window is None:
            if len(self._failures) >= self.max_tracked:
                # Evict the oldest buckets; they are the least likely to be attacked.
                for stale_key in list(self._failures)[: self.max_tracked // 4]:
                    self._failures.pop(stale_key, None)
            window = deque()
            self._failures[tracked] = window
        self._prune(window, now)
        window.append(now)
        return len(window)

    def clear(self, key: str) -> None:
        """Reset the failure bucket (after a successful login)."""
        self._failures.pop(self._key(key), None)

    def retry_after(self, key: str, now: float) -> int:
        """Seconds until the oldest recorded failure leaves the window."""
        window = self._failures.get(self._key(key))
        if not window:
            return int(self.window_seconds)
        return max(1, int(round(window[0] + self.window_seconds - now)))

    def failure_count(self, key: str) -> int:
        """Number of currently tracked failures for ``key``."""
        window = self._failures.get(self._key(key))
        return len(window) if window else 0


#: The desk-login limiter (``POST /api/auth/login``).
_limiter = FailedLoginLimiter()
#: Backwards-compatible alias: the raw bucket store used by the module functions.
_failures: dict[str, deque[float]] = _limiter._failures


def _prune(window: deque[float], now: float) -> None:
    """Drop timestamps that fell out of the sliding window."""
    _limiter._prune(window, now)


def _is_rate_limited(key: str, now: float) -> bool:
    """Return ``True`` when ``key`` exhausted its failure budget."""
    return _limiter.is_limited(key, now)


def _record_failure(key: str, now: float) -> None:
    """Register one failed attempt for ``key``."""
    _limiter.record_failure(key, now)


def _clear_failures(key: str) -> None:
    """Reset the failure bucket after a successful login."""
    _limiter.clear(key)


def _retry_after_seconds(key: str, now: float) -> int:
    """Seconds until the oldest recorded failure leaves the window."""
    return _limiter.retry_after(key, now)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for a JWT access token",
    responses={
        401: {"description": "Invalid username or password"},
        429: {"description": "Too many failed login attempts"},
    },
)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LoginResponse:
    """Authenticate an operator and return a bearer token plus its profile."""
    username = (payload.username or "").strip()
    password = payload.password or ""
    rate_key = username.casefold()
    now = time.monotonic()

    if _is_rate_limited(rate_key, now):
        retry_after = _retry_after_seconds(rate_key, now)
        logger.warning("login throttled for %r (%ss remaining)", username, retry_after)
        raise HTTPException(
            status_code=TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    operator = await _resolve_operator(db, username)
    if operator is None or not _verify(password, operator.password_hash):
        _record_failure(rate_key, now)
        logger.info("failed login for %r (attempt %s)", username, len(_failures.get(rate_key, ())))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if operator.username == str(getattr(settings, "backend_supervisor_username", "") or "").strip():
        await sync_supervisor_password(db, operator)

    operator.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(operator)
    _clear_failures(rate_key)

    token = create_access_token(
        operator.id,
        username=operator.username,
        role=str(getattr(operator.role, "value", operator.role)),
    )
    expires_in = int(getattr(settings, "backend_access_token_expire_minutes", 720) or 720) * 60

    await safe_broadcast("OPERATOR_PRESENCE", _presence_data(operator))
    logger.info("operator %r logged in (id=%s)", operator.username, operator.id)

    return LoginResponse.model_validate(
        {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "operator": operator_out(operator),
        }
    )


@router.get(
    "/me",
    response_model=OperatorOut,
    status_code=status.HTTP_200_OK,
    summary="Return the authenticated operator profile",
)
async def read_me(
    operator: Annotated[Operator, Depends(get_current_operator)],
) -> OperatorOut:
    """Return the calling operator as ``OperatorOut``."""
    return operator_out(operator)


@router.post(
    "/logout",
    response_model=OkOut,
    status_code=status.HTTP_200_OK,
    summary="Discard the current session",
)
async def logout(
    operator: Annotated[Operator, Depends(get_current_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    """Client-side token discard; clears ``is_online_ws`` when no socket remains."""
    sockets_left = _connection_count(operator.id)
    operator.is_online_ws = sockets_left > 0
    operator.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(operator)

    await safe_broadcast("OPERATOR_PRESENCE", _presence_data(operator))
    logger.info(
        "operator %r logged out (%s socket(s) still open)", operator.username, sockets_left
    )
    return OkOut.model_validate({"ok": True})


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _resolve_operator(db: AsyncSession, username: str) -> Operator | None:
    """Load the operator row, provisioning the supervisor on a virgin database."""
    if not username:
        return None
    operator = (await db.execute(select(Operator).where(Operator.username == username))).scalar_one_or_none()
    if operator is not None:
        return operator

    supervisor = str(getattr(settings, "backend_supervisor_username", "") or "").strip()
    if username == supervisor:
        # Fresh database (``seed.py`` not run yet): create the configured supervisor.
        created = await ensure_supervisor(db)
        if created is not None and created.username == username:
            return created
    logger.info("login attempt for unknown username %r", username)
    return None


def _verify(password: str, stored_hash: str | None) -> bool:
    """Timing-safe password check that never raises."""
    if not stored_hash:
        return False
    try:
        return bool(verify_password(password, stored_hash))
    except Exception as exc:  # noqa: BLE001 - unreadable hashes are simply invalid
        logger.warning("password hash verification failed: %s", exc)
        return False


def _connection_count(operator_id: int) -> int:
    """Live WebSocket sockets held by ``operator_id`` (``0`` when unavailable)."""
    try:
        from app.ws_manager import manager

        return int(manager.operator_connection_count(operator_id))
    except Exception as exc:  # noqa: BLE001 - presence is cosmetic here
        logger.debug("operator_connection_count unavailable: %s", exc)
        return 0


def _presence_data(operator: Operator) -> dict[str, object]:
    """Build the ``OPERATOR_PRESENCE`` payload (§4)."""
    return {
        "operator_id": int(operator.id),
        "is_online_ws": bool(operator.is_online_ws),
        "shift_status": str(getattr(operator.shift_status, "value", operator.shift_status)),
    }

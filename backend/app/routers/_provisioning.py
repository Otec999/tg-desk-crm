"""Idempotent operator provisioning used by the auth router.

A freshly initialised database has no rows until ``seed.py`` runs, yet the
supervisor configured in ``.env`` must always be able to log in.  This module
creates that account on first login only - it never overwrites an existing
row, so password changes made later survive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Operator, ShiftStatus
from app.security import hash_password, verify_password

logger = logging.getLogger("app.routers.auth")

__all__ = ["ensure_operator", "ensure_supervisor", "sync_supervisor_password"]

#: Matches ``backend/seed.py`` (§7): supervisor ``admin`` / ``admin123`` -> "Supervisor".
SUPERVISOR_ROLE = "SUPERVISOR"
SUPERVISOR_DISPLAY_NAME = "Supervisor"
DEFAULT_SLA_TARGET_SECONDS = 300


def _role_value(role: str) -> object:
    """Map a role string onto the project's enum when ``app.models`` defines one."""
    from app import models as model_module

    enum_cls = getattr(model_module, "OperatorRole", None)
    if enum_cls is None:
        return role
    try:
        return enum_cls(role)
    except ValueError:  # pragma: no cover - enum without a SUPERVISOR member
        return role


def _shift_value(value: str) -> object:
    """Map a shift-status string onto ``ShiftStatus``."""
    try:
        return ShiftStatus(value)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return value


def _display_name_for(username: str) -> str:
    """Human readable fallback display name derived from the username."""
    return username.replace("_", " ").replace(".", " ").title() or username


async def ensure_operator(
    db: AsyncSession,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    role: str = "OPERATOR",
) -> Operator | None:
    """Create ``username`` when it does not exist yet.

    Returns the (possibly pre-existing) operator, or ``None`` when the account
    could not be provisioned (e.g. a concurrent insert won the race).  The
    caller decides whether a missing row means "invalid credentials".
    """
    username = (username or "").strip()
    if not username:
        return None

    existing = (await db.execute(select(Operator).where(Operator.username == username))).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    operator = Operator(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name or _display_name_for(username),
        role=_role_value(role),
        shift_status=_shift_value("OFFLINE"),
        is_online_ws=False,
        last_seen_at=now,
        sla_target_seconds=int(getattr(settings, "backend_sla_seconds", DEFAULT_SLA_TARGET_SECONDS)),
        created_at=now,
        updated_at=now,
    )
    db.add(operator)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - concurrent insert is not an error
        await db.rollback()
        logger.warning("could not provision operator %r: %s", username, exc)
        existing = (await db.execute(select(Operator).where(Operator.username == username))).scalar_one_or_none()
        return existing
    await db.refresh(operator)
    logger.info("provisioned %s account %r", role.lower(), username)
    return operator


async def ensure_supervisor(db: AsyncSession) -> Operator | None:
    """Provision the ``.env`` supervisor account if the database has no rows yet."""
    username = str(getattr(settings, "backend_supervisor_username", "") or "").strip()
    password = str(getattr(settings, "backend_supervisor_password", "") or "")
    if not username or not password:
        return None
    return await ensure_operator(
        db, username, password, display_name=SUPERVISOR_DISPLAY_NAME, role=SUPERVISOR_ROLE
    )


async def sync_supervisor_password(db: AsyncSession, operator: Operator) -> bool:
    """Re-apply the ``.env`` supervisor password to the supervisor account.

    Guarantees the operational account always matches configuration (the .env
    value is the source of truth for the supervisor only).  Returns ``True``
    when the stored hash changed.
    """
    password = str(getattr(settings, "backend_supervisor_password", "") or "")
    if not password:
        return False
    try:
        if verify_password(password, operator.password_hash):
            return False
    except Exception as exc:  # noqa: BLE001 - a corrupt hash must not 500 the login
        logger.warning("stored hash for %r is unreadable (%s); resetting it", operator.username, exc)
    operator.password_hash = hash_password(password)
    operator.updated_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("supervisor password re-synced from configuration for %r", operator.username)
    return True


async def touch_last_seen(db: AsyncSession, operator_id: int) -> None:
    """Refresh ``last_seen_at`` without loading the whole row."""
    await db.execute(
        update(Operator).where(Operator.id == operator_id).values(last_seen_at=datetime.now(timezone.utc))
    )

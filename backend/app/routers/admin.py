"""Admin panel router (CONTRACTS_V2.md В§3.3) вЂ” ``/api/admin``.

The panel is a **separate login form**: ``POST /api/admin/login`` mints a token with
``scope="admin"`` (see :func:`app.security.create_admin_token`) and only that scope is
accepted by the dependencies below. A desk token from ``/api/auth/login`` is refused
(401), and an admin token is likewise useless on the desk endpoints вЂ” the two
permission domains never overlap.

Access matrix (В§3.3, В«РћРіСЂР°РЅРёС‡РµРЅРёСЏВ»):

============================  =====================================
endpoint                      required dependency
============================  =====================================
``POST   /login``             вЂ” (rate limited)
``GET    /operators``         ``require_privileged`` (ADMIN, SUPERVISOR)
``POST   /operators``         ``require_admin``
``PATCH  /operators/{id}``    ``require_admin``
``DELETE /operators/{id}``    ``require_admin``
``POST   /operators/{id}/reset-password``  ``require_admin``
``GET    /accounts``          ``require_admin``
``POST   /accounts``          ``require_admin``
``PATCH  /accounts/{id}``     ``require_admin``
``DELETE /accounts/{id}``     ``require_admin``
``GET    /audit``             ``require_admin``
``GET    /settings``          ``require_admin``   (CONTRACTS_V5 В§2.2)
``PATCH  /settings``          ``require_admin``   (CONTRACTS_V5 В§2.2)
============================  =====================================

``POST /login`` accepts **only** the ``ADMIN`` role: a supervisor or operator who
guesses the URL still has no way in, and the failure text stays identical to
``/api/auth/login`` so the endpoint cannot be used to enumerate accounts.

Rate limiting reuses :mod:`app.routers.auth`'s limiter (same window, same budget,
same ``429`` body and ``Retry-After`` header) so the admin form cannot be brute-forced
either. Successful admin writes fan out ``ADMIN_OPERATOR_CHANGED`` (В§4).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.enums import OperatorRole
from app.models import Operator
from app.routers._common import safe_broadcast
from app.routers.auth import (
    INVALID_CREDENTIALS,
    RATE_LIMIT_MAX_FAILURES,
    RATE_LIMIT_WINDOW_SECONDS,
    TOO_MANY_REQUESTS,
    FailedLoginLimiter,
)
from app.schemas import (
    AdminLoginRequest,
    AdminSettingsOut,
    AdminSettingsUpdate,
    AuditEntryOut,
    AuditListOut,
    OkOut,
    OperatorAdminListOut,
    OperatorAdminOut,
    OperatorCreateRequest,
    OperatorUpdateRequest,
    PasswordResetRequest,
    TelegramAccountCreateRequest,
    TelegramAccountListOut,
    TelegramAccountOut,
    TelegramAccountUpdateRequest,
    UtcDatetime,
    _ORMModel,
)
from app.security import (
    ADMIN_REQUIRED_DETAIL,
    create_admin_token,
    is_active_operator,
    require_admin,
    require_privileged,
    verify_password,
)
from app.services import admin as admin_service
from app.services import settings_service
from app.services.admin import (
    AUDIT_TARGET_OPERATOR,
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
)

logger = logging.getLogger("tgdesk.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

__all__ = ["AdminLoginResponse", "router"]

#: Who may authenticate through ``POST /api/admin/login``. В§3.3 grants the panel a
#: dedicated form, so supervisors keep their roster read-only access but still log
#: in through the desk form вЂ” only the ADMIN role passes here.
ADMIN_LOGIN_ROLES: frozenset[str] = frozenset({OperatorRole.ADMIN.value})

#: Roles В§3.3 lets into the panel *when the token was minted by ``/api/admin/login``*.
#: A SUPERVISOR reaching ``GET /api/admin/operators`` needs an admin-scope token
#: (``require_privileged`` rejects desk tokens on purpose), and this endpoint was the
#: only issuer вЂ” with a hard-coded ``ADMIN`` gate the read-only supervisor access
#: the contract grants was unreachable. Supervisors now get a read-only token; every
#: mutating route still goes through ``require_admin`` and stays closed to them.
PANEL_LOGIN_ROLES: frozenset[str] = frozenset(
    {OperatorRole.ADMIN.value, OperatorRole.SUPERVISOR.value}
)

#: Independent failure budget for the admin form. Same window and budget as
#: ``/api/auth/login`` (the router is expected to be immune to brute-forcing) but a
#: **separate** bucket, so hammering the desk login form can never lock the
#: administrator out of the recovery panel вЂ” or vice versa.
_admin_limiter = FailedLoginLimiter(
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    max_failures=RATE_LIMIT_MAX_FAILURES,
    max_tracked=2048,
    namespace="admin",
)


class AdminLoginResponse(_ORMModel):
    """``POST /api/admin/login`` 200 response.

    Mirrors :class:`app.schemas.LoginResponse` except that the lifetime is reported
    as an absolute ``expires_at`` (the admin panel shows a session clock) rather
    than ``expires_in``, and the profile is the admin DTO.
    """

    access_token: str
    token_type: str = "bearer"
    expires_at: UtcDatetime
    operator: OperatorAdminOut
    scope: str = Field(default="admin")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _operator_admin_out(operator: Operator) -> OperatorAdminOut:
    """Project an ``Operator`` row onto the admin DTO (never the password hash)."""
    return OperatorAdminOut(
        id=int(operator.id),
        username=str(operator.username),
        display_name=str(getattr(operator, "display_name", "") or ""),
        role=str(getattr(operator, "role", OperatorRole.OPERATOR.value)),
        shift_status=str(getattr(operator, "shift_status", "") or ""),
        is_active=is_active_operator(operator),
        max_chats=getattr(operator, "max_chats", None),
        can_delete_messages=bool(getattr(operator, "can_delete_messages", False)),
        can_export=bool(getattr(operator, "can_export", False)),
        avatar_color=getattr(operator, "avatar_color", None),
        created_at=getattr(operator, "created_at", None),
        updated_at=getattr(operator, "updated_at", None),
    )


def _account_out(account: Any) -> TelegramAccountOut:
    """Project a masked account view (from ``services.admin``) onto the DTO."""
    return TelegramAccountOut(
        id=int(account.id),
        label=str(account.label),
        tg_user_id=getattr(account, "tg_user_id", None),
        tg_username=getattr(account, "tg_username", None),
        phone=getattr(account, "phone", None),
        api_id=getattr(account, "api_id", None),
        is_active=bool(getattr(account, "is_active", True)),
        has_session=bool(getattr(account, "has_session", False)),
        created_at=getattr(account, "created_at", None),
        updated_at=getattr(account, "updated_at", None),
    )


def _audit_out(entry: Any) -> AuditEntryOut:
    """Project an ``AuditEntry`` row (whose ``actor`` is eagerly loaded) onto the DTO."""
    actor = getattr(entry, "actor", None)
    return AuditEntryOut(
        id=int(entry.id),
        actor_id=getattr(entry, "actor_id", None),
        actor_username=getattr(actor, "username", None) if actor is not None else None,
        action=str(entry.action),
        target_type=getattr(entry, "target_type", None),
        target_id=getattr(entry, "target_id", None),
        detail=getattr(entry, "detail", None),
        created_at=getattr(entry, "created_at", None),
    )


async def _notify_operator_changed(operator: Operator, action: str) -> None:
    """Fan out ``ADMIN_OPERATOR_CHANGED`` (В§4) вЂ” purely advisory, never fatal."""
    await safe_broadcast(
        "ADMIN_OPERATOR_CHANGED",
        {
            "operator_id": int(operator.id),
            "action": action,
            "username": str(operator.username),
            "role": str(getattr(operator, "role", OperatorRole.OPERATOR.value)),
            "is_active": is_active_operator(operator),
        },
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Р’С…РѕРґ РІ РїР°РЅРµР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР° (РѕС‚РґРµР»СЊРЅС‹Р№ С‚РѕРєРµРЅ СЃРѕ scope=admin)",
    responses={
        401: {"description": "РќРµРІРµСЂРЅРѕРµ РёРјСЏ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РёР»Рё РїР°СЂРѕР»СЊ"},
        403: {"description": ADMIN_REQUIRED_DETAIL},
        429: {"description": "РЎР»РёС€РєРѕРј РјРЅРѕРіРѕ РЅРµСѓРґР°С‡РЅС‹С… РїРѕРїС‹С‚РѕРє РІС…РѕРґР°"},
    },
)
async def admin_login(
    payload: AdminLoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminLoginResponse:
    """Authenticate an administrator and return an admin-scope bearer token.

    Failures are deliberately indistinguishable:

    * unknown username / wrong password -> ``401`` with the exact text used by
      ``/api/auth/login`` (no account enumeration);
    * valid credentials but a non-ADMIN account -> ``403`` В«РўСЂРµР±СѓСЋС‚СЃСЏ РїСЂР°РІР°
      Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°В» (the operator already proved ownership of the account, so
      this leaks nothing they do not own);
    * a disabled account -> the same ``401`` as bad credentials, so a blocked
      operator cannot distinguish "blocked" from "wrong password".
    """
    username = str(payload.username or "").strip().lower()
    password = payload.password or ""
    rate_key = username
    now = time.monotonic()

    if _admin_limiter.is_limited(rate_key, now):
        retry_after = _admin_limiter.retry_after(rate_key, now)
        logger.warning("admin login throttled for %r (%ss remaining)", username, retry_after)
        raise HTTPException(
            status_code=TOO_MANY_REQUESTS,
            detail=f"РЎР»РёС€РєРѕРј РјРЅРѕРіРѕ РЅРµСѓРґР°С‡РЅС‹С… РїРѕРїС‹С‚РѕРє РІС…РѕРґР°. РџРѕРІС‚РѕСЂРёС‚Рµ С‡РµСЂРµР· {retry_after} СЃ.",
            headers={"Retry-After": str(retry_after)},
        )

    operator = await _resolve_admin_candidate(db, username)
    if operator is None or not _verify(password, operator.password_hash):
        _admin_limiter.record_failure(rate_key, now)
        logger.info("failed admin login for %r", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    if not is_active_operator(operator):
        # Same 401 as a wrong password: a blocked account learns nothing.
        _admin_limiter.record_failure(rate_key, now)
        logger.info("admin login refused for deactivated account %r", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS)

    role = str(getattr(operator, "role", "") or "")
    if role not in PANEL_LOGIN_ROLES:
        logger.warning(
            "admin login refused for %r: role %s is not allowed into the admin form",
            username,
            role,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_REQUIRED_DETAIL)

    # ``create_admin_token`` signs ``scope="admin"`` with the operator's own role, so
    # the resulting token is accepted only by endpoints whose dependency matches that
    # role: a SUPERVISOR token reaches ``require_privileged`` (roster read) and is
    # refused by ``require_admin`` / ``decode_admin_token`` (every mutation).
    token, expires_at = create_admin_token(operator)
    _admin_limiter.clear(rate_key)

    await admin_service.record_audit(
        db,
        actor=operator,
        action="admin_login",
        target_type=AUDIT_TARGET_OPERATOR,
        target_id=operator.id,
        detail="Р’С…РѕРґ РІ РїР°РЅРµР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°",
    )
    await db.commit()

    logger.info("administrator %r logged in (id=%s)", operator.username, operator.id)
    return AdminLoginResponse(
        access_token=token,
        token_type="bearer",
        expires_at=expires_at,
        operator=_operator_admin_out(operator),
    )


async def _resolve_admin_candidate(db: AsyncSession, username: str) -> Operator | None:
    """Load the operator row for ``username`` (``None`` when absent)."""
    if not username:
        return None
    result = await db.execute(select(Operator).where(Operator.username == username).limit(1))
    return result.scalar_one_or_none()


def _verify(password: str, stored_hash: str | None) -> bool:
    """Timing-safe password check that never raises."""
    if not stored_hash:
        return False
    try:
        return bool(verify_password(password, stored_hash))
    except Exception as exc:  # noqa: BLE001 - an unreadable hash is simply invalid
        logger.warning("admin password verification failed (%s)", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


@router.get(
    "/operators",
    response_model=OperatorAdminListOut,
    status_code=status.HTTP_200_OK,
    summary="РЎРїРёСЃРѕРє РѕРїРµСЂР°С‚РѕСЂРѕРІ (ADMIN Рё SUPERVISOR)",
)
async def list_operators(
    _viewer: Annotated[Operator, Depends(require_privileged)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAdminListOut:
    """Return every operator account, deactivated ones included."""
    operators = await admin_service.list_operators(db)
    return OperatorAdminListOut(items=[_operator_admin_out(item) for item in operators])


@router.post(
    "/operators",
    response_model=OperatorAdminOut,
    status_code=status.HTTP_201_CREATED,
    summary="РЎРѕР·РґР°С‚СЊ РѕРїРµСЂР°С‚РѕСЂР°",
    responses={409: {"description": "РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ СЃ С‚Р°РєРёРј РёРјРµРЅРµРј СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚"}},
)
async def create_operator(
    payload: OperatorCreateRequest,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAdminOut:
    """Create an operator account and journal the action."""
    operator = await admin_service.create_operator(db, payload, actor=actor)
    await _notify_operator_changed(operator, "created")
    return _operator_admin_out(operator)


@router.patch(
    "/operators/{operator_id}",
    response_model=OperatorAdminOut,
    status_code=status.HTTP_200_OK,
    summary="РР·РјРµРЅРёС‚СЊ РѕРїРµСЂР°С‚РѕСЂР° (username РЅРµРёР·РјРµРЅСЏРµРј)",
    responses={
        404: {"description": "РћРїРµСЂР°С‚РѕСЂ РЅРµ РЅР°Р№РґРµРЅ"},
        409: {"description": "РќРµР»СЊР·СЏ РёР·РјРµРЅРёС‚СЊ РїРѕСЃР»РµРґРЅРµРіРѕ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°"},
    },
)
async def update_operator(
    operator_id: int,
    payload: OperatorUpdateRequest,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAdminOut:
    """Partially update an operator; a ``username`` field would be ignored."""
    operator = await admin_service.update_operator(db, operator_id, payload, actor=actor)
    await _notify_operator_changed(operator, "updated")
    return _operator_admin_out(operator)


@router.delete(
    "/operators/{operator_id}",
    response_model=OperatorAdminOut,
    status_code=status.HTTP_200_OK,
    summary="Р”РµР°РєС‚РёРІРёСЂРѕРІР°С‚СЊ РѕРїРµСЂР°С‚РѕСЂР° (РјСЏРіРєРѕРµ СѓРґР°Р»РµРЅРёРµ)",
    responses={
        404: {"description": "РћРїРµСЂР°С‚РѕСЂ РЅРµ РЅР°Р№РґРµРЅ"},
        409: {"description": "РќРµР»СЊР·СЏ РёР·РјРµРЅРёС‚СЊ РїРѕСЃР»РµРґРЅРµРіРѕ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°"},
    },
)
async def deactivate_operator(
    operator_id: int,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAdminOut:
    """Soft-delete: ``is_active=False``; the row and its history are kept."""
    operator = await admin_service.deactivate_operator(db, operator_id, actor=actor)
    await _notify_operator_changed(operator, "deleted")
    return _operator_admin_out(operator)


@router.post(
    "/operators/{operator_id}/reset-password",
    response_model=OkOut,
    status_code=status.HTTP_200_OK,
    summary="РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ РѕРїРµСЂР°С‚РѕСЂР°",
    responses={404: {"description": "РћРїРµСЂР°С‚РѕСЂ РЅРµ РЅР°Р№РґРµРЅ"}},
)
async def reset_password(
    operator_id: int,
    payload: PasswordResetRequest,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    """Replace an operator's password; the value is never echoed back."""
    await admin_service.reset_password(db, operator_id, payload.new_password, actor=actor)
    return OkOut.model_validate({"ok": True})


# ---------------------------------------------------------------------------
# Telegram accounts
# ---------------------------------------------------------------------------


@router.get(
    "/accounts",
    response_model=TelegramAccountListOut,
    status_code=status.HTTP_200_OK,
    summary="РЎРїРёСЃРѕРє Telegram-Р°РєРєР°СѓРЅС‚РѕРІ (СЃРµРєСЂРµС‚С‹ Р·Р°РјР°СЃРєРёСЂРѕРІР°РЅС‹)",
)
async def list_accounts(
    _actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TelegramAccountListOut:
    """Return every account; only ``has_session`` is exposed, never the session."""
    accounts = await admin_service.list_accounts(db)
    return TelegramAccountListOut(items=[_account_out(item) for item in accounts])


@router.post(
    "/accounts",
    response_model=TelegramAccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="РЎРѕР·РґР°С‚СЊ Telegram-Р°РєРєР°СѓРЅС‚",
    responses={409: {"description": "РђРєРєР°СѓРЅС‚ СЃ С‚Р°РєРёРј РЅР°Р·РІР°РЅРёРµРј СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚"}},
)
async def create_account(
    payload: TelegramAccountCreateRequest,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TelegramAccountOut:
    """Store a new account with its secrets Fernet-encrypted at rest."""
    account = await admin_service.create_account(db, payload, actor=actor)
    return _account_out(account)


@router.patch(
    "/accounts/{account_id}",
    response_model=TelegramAccountOut,
    status_code=status.HTTP_200_OK,
    summary="РР·РјРµРЅРёС‚СЊ Telegram-Р°РєРєР°СѓРЅС‚",
    responses={404: {"description": "Telegram-Р°РєРєР°СѓРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}},
)
async def update_account(
    account_id: int,
    payload: TelegramAccountUpdateRequest,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TelegramAccountOut:
    """Partially update an account; new secrets are re-encrypted, ``null`` clears them."""
    account = await admin_service.update_account(db, account_id, payload, actor=actor)
    return _account_out(account)


@router.delete(
    "/accounts/{account_id}",
    response_model=OkOut,
    status_code=status.HTTP_200_OK,
    summary="РЈРґР°Р»РёС‚СЊ Telegram-Р°РєРєР°СѓРЅС‚",
    responses={404: {"description": "Telegram-Р°РєРєР°СѓРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"}},
)
async def delete_account(
    account_id: int,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    """Delete the account row (messages/audit do not reference accounts)."""
    await admin_service.delete_account(db, account_id, actor=actor)
    return OkOut.model_validate({"ok": True})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get(
    "/audit",
    response_model=AuditListOut,
    status_code=status.HTTP_200_OK,
    summary="Р–СѓСЂРЅР°Р» РґРµР№СЃС‚РІРёР№ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРІ",
)
async def list_audit(
    _actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LIMIT)] = DEFAULT_AUDIT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_type: Annotated[
        Literal["operator", "telegram_account", "settings"] | None, Query()
    ] = None,
) -> AuditListOut:
    """Return the append-only journal, newest first, with optional filters."""
    entries = await admin_service.list_audit(
        db, limit=limit, offset=offset, action=action, target_type=target_type
    )
    return AuditListOut(items=[_audit_out(entry) for entry in entries])


# ---------------------------------------------------------------------------
# SLA settings (CONTRACTS_V5 В§2.2)
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=AdminSettingsOut,
    status_code=status.HTTP_200_OK,
    summary="РќР°СЃС‚СЂРѕР№РєРё SLA (С‚РѕР»СЊРєРѕ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ)",
)
async def get_settings(
    _viewer: Annotated[Operator, Depends(require_privileged)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminSettingsOut:
    """Return the singleton ``app_settings`` row (В§2.2).

    The row is bootstrapped with the documented defaults on first read, so the panel can
    open on a fresh database without a separate seed step.

    Read is open to ``require_privileged`` (ADMIN **and** SUPERVISOR), mirroring the
    roster and audit endpoints: a supervisor sees the effective SLA policy but cannot
    change it. The write below stays ``require_admin`` — the switch alters global
    behaviour (§2.2, §2.3). With an admin-only gate the SLA tab rendered empty (403)
    for supervisors instead of the read-only values that shape their own deadlines.
    """
    row = await settings_service.get_settings(db)
    return AdminSettingsOut.from_settings(row)


@router.patch(
    "/settings",
    response_model=AdminSettingsOut,
    status_code=status.HTTP_200_OK,
    summary="РР·РјРµРЅРёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё SLA (С‚РѕР»СЊРєРѕ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ)",
    responses={
        422: {"description": "sla_target_seconds РІРЅРµ РґРёР°РїР°Р·РѕРЅР° 60..86400"},
    },
)
async def patch_settings(
    payload: AdminSettingsUpdate,
    actor: Annotated[Operator, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminSettingsOut:
    """Apply a partial settings update and return the persisted row (В§2.2/В§2.3).

    Only the fields present in the request body are written. The write is journaled in
    ``admin_audit_log`` inside the same transaction (В§2.3 В«РїРµСЂРµРєР»СЋС‡РµРЅРёРµ ``sla_enabled``
    РїРёС€РµС‚СЃСЏ РІ ``admin_audit_log``В») and invalidates the 5 s read cache, so the next
    inbound message already observes the new values.
    """
    row = await settings_service.update_settings(
        db,
        payload=payload,
        actor=actor.username,
        actor_id=actor.id,
    )
    logger.info(
        "РЅР°СЃС‚СЂРѕР№РєРё SLA РёР·РјРµРЅРµРЅС‹ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРј %r (sla_enabled=%s, target=%ss, reset=%s)",
        actor.username,
        row.sla_enabled,
        row.sla_target_seconds,
        row.sla_reset_on_operator_reply,
    )
    return AdminSettingsOut.from_settings(row)

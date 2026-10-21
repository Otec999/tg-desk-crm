"""Admin-panel domain services (CONTRACTS_V2.md §3.3).

This module owns every write an administrator can perform: operator accounts,
Telegram accounts and the append-only ``admin_audit_log`` journal. The router in
:mod:`app.routers.admin` is a thin HTTP shell over these functions, so the same
rules apply no matter who calls them (REST, a CLI script, a future worker).

Rules enforced here (and why)
-----------------------------
1. **The last active administrator cannot be demoted or deactivated.** Otherwise
   the installation would have nobody able to reach ``/api/admin`` ever again.
   The check runs inside a transaction that pins every administrator row with
   ``SELECT ... FOR UPDATE`` *before* counting, so two concurrent demotions can
   never both observe "there are two admins" and both succeed.
2. **``username`` is immutable.** It is the natural key used by the login form and
   by ``tx_lookup_audit``; renaming would silently break both. ``update_operator``
   simply ignores the field — there is nothing to "change it to".
3. **Passwords only ever travel through :func:`app.security.hash_password`.** They
   are never logged, never returned and never stored in the audit journal.
4. **Duplicate ``username`` / account ``label``** answer ``409`` with a Russian
   message instead of a raw ``IntegrityError``.
5. **Telegram secrets are Fernet-encrypted at rest** with a key derived from
   ``settings.secret_box_key`` (§7: SHA-256 of ``BACKEND_SECRET_KEY`` when unset).
   :func:`mask_secrets` is the only shape allowed to leave the process: it reports
   ``has_session``/``has_api_hash`` booleans and never the ciphertext or plaintext.
6. **Every mutation writes one audit row.** The journal is written inside the same
   transaction as the change, so an action and its audit entry are atomic.
7. **Deletion is always soft.** ``deactivate_operator`` flips ``is_active`` only:
   messages, SLA history and audit rows keep their foreign keys.

Transactionality: the router's ``get_db`` session is used as-is. Each public
function commits once, at the end, after the audit row is staged; on any error the
session is rolled back and the exception is re-raised unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OperatorRole
from app.models import (
    AUDIT_ACTION_CREATE_ACCOUNT,
    AUDIT_ACTION_CREATE_OPERATOR,
    AUDIT_ACTION_DEACTIVATE_OPERATOR,
    AUDIT_ACTION_DELETE_ACCOUNT,
    AUDIT_ACTION_RESET_PASSWORD,
    AUDIT_ACTION_UPDATE_ACCOUNT,
    AUDIT_ACTION_UPDATE_OPERATOR,
    AUDIT_ACTION_UPDATE_SETTINGS,
    DEFAULT_SLA_TARGET_SECONDS,
    AuditEntry,
    Operator,
    TelegramAccount,
)
from app.security import hash_password, is_active_operator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.schemas import (
        OperatorCreateRequest,
        OperatorUpdateRequest,
        TelegramAccountCreateRequest,
        TelegramAccountUpdateRequest,
    )

logger = logging.getLogger("tgdesk.admin")

__all__ = [
    "AUDIT_TARGET_OPERATOR",
    "AUDIT_TARGET_SETTINGS",
    "AUDIT_TARGET_TELEGRAM_ACCOUNT",
    "AUDIT_ACTION_UPDATE_SETTINGS",
    "DEFAULT_AUDIT_LIMIT",
    "MAX_AUDIT_LIMIT",
    "TelegramAccountView",
    "create_account",
    "create_operator",
    "deactivate_operator",
    "decrypt_secret",
    "delete_account",
    "encrypt_secret",
    "get_operator",
    "list_accounts",
    "list_audit",
    "list_operators",
    "mask_secrets",
    "record_audit",
    "reset_password",
    "update_account",
    "update_operator",
]

#: ``admin_audit_log.target_type`` discriminators.
AUDIT_TARGET_OPERATOR: Final[str] = "operator"
AUDIT_TARGET_TELEGRAM_ACCOUNT: Final[str] = "telegram_account"
#: CONTRACTS_V5 §2.3: the ``app_settings`` singleton (``target_id`` is always ``1``).
#: Serves as both the ``action`` and the ``target_type`` of a settings write — the journal
#: row then reads «update_settings → settings:1», which is unambiguous without a new
#: ``operator``-shaped target. Deliberately a plain module constant (not an entry in
#: ``app.models.AUDIT_ACTION_VALUES``) because ``models.py`` is owned by another agent;
#: ``AuditEntry.action`` is a free ``VARCHAR(64)`` and ``GET /api/admin/audit`` filters on
#: the literal string, so the journal stays fully queryable.
AUDIT_TARGET_SETTINGS: Final[str] = "settings"

#: ``GET /api/admin/audit`` default page size and hard ceiling.
DEFAULT_AUDIT_LIMIT: Final[int] = 200
MAX_AUDIT_LIMIT: Final[int] = 500

#: Error texts (Russian — CONTRACTS_V2.md §0: user-facing strings are Russian).
_MSG_NOT_FOUND_OPERATOR = "Оператор не найден"
_MSG_NOT_FOUND_ACCOUNT = "Telegram-аккаунт не найден"
_MSG_DUPLICATE_USERNAME = "Пользователь с таким именем уже существует"
_MSG_DUPLICATE_LABEL = "Аккаунт с таким названием уже существует"
_MSG_LAST_ADMIN = "Нельзя изменить последнего администратора"
_MSG_NO_CIPHER = "Шифрование секретов недоступно: установите пакет cryptography или задайте SECRET_BOX_KEY"

#: Fernet key length in bytes (AES-128 in CBC + HMAC-SHA256).
_FERNET_KEY_BYTES: Final[int] = 32

#: Fallback signing/derivation secret, identical in spirit to ``app.security``.
#: It is only used when neither ``SECRET_BOX_KEY`` nor ``BACKEND_SECRET_KEY`` is set,
#: i.e. on an unconfigured dev box, so the module never crashes on import.
_DEV_FALLBACK_SECRET: Final[str] = "tg-desk-dev-only-insecure-secret-change-me"

#: ``Operator`` attributes an administrator may change through ``PATCH``.
_OPERATOR_EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "display_name",
    "role",
    "is_active",
    "max_chats",
    "can_delete_messages",
    "can_export",
    "avatar_color",
    # CONTRACTS_V5 §2.3: the per-operator override is the highest-priority SLA target,
    # so an administrator must be able to set it — otherwise the documented priority
    # chain (operator -> app_settings -> config) is unreachable through the API.
    "sla_target_seconds",
)

#: ``TelegramAccount`` attributes an administrator may change through ``PATCH``.
#: The secrets are handled separately (see :func:`_apply_account_secrets`).
_ACCOUNT_EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "label",
    "tg_user_id",
    "tg_username",
    "phone",
    "api_id",
    "is_active",
)


class TelegramAccountView:
    """A ``TelegramAccount`` row plus its **masked** secret flags.

    ``GET/POST/PATCH /api/admin/accounts`` answer ``TelegramAccountOut``, which asks
    for ``has_session`` — a property of :class:`~app.models.TelegramAccount`. A
    read-only view keeps the ORM entity free of response-only concerns and makes
    "secrets never leave in clear" structurally true: the only secret-shaped values
    here are booleans.
    """

    __slots__ = ("_account", "has_api_hash", "has_session")

    def __init__(self, account: TelegramAccount, *, has_session: bool, has_api_hash: bool) -> None:
        self._account = account
        self.has_session = has_session
        self.has_api_hash = has_api_hash

    # -- columns proxied from the row ---------------------------------------
    @property
    def id(self) -> int:
        return self._account.id

    @property
    def label(self) -> str:
        return self._account.label

    @property
    def tg_user_id(self) -> int | None:
        return self._account.tg_user_id

    @property
    def tg_username(self) -> str | None:
        return self._account.tg_username

    @property
    def phone(self) -> str | None:
        return self._account.phone

    @property
    def api_id(self) -> int | None:
        return self._account.api_id

    @property
    def is_active(self) -> bool:
        return bool(self._account.is_active)

    @property
    def created_at(self) -> datetime | None:
        return self._account.created_at

    @property
    def updated_at(self) -> datetime | None:
        return self._account.updated_at

    def __repr__(self) -> str:  # never includes the session/api_hash values
        return (
            f"<TelegramAccountView id={self.id!r} label={self.label!r} "
            f"has_session={self.has_session!r}>"
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Timezone-aware UTC "now" (single source of truth for audit timestamps)."""
    return datetime.now(timezone.utc)


def _resolve_role(operator: Operator) -> str:
    """Return the operator's role as its canonical ``str`` value."""
    return str(getattr(operator, "role", "") or "")


def _actor_label(actor: Operator | None) -> str:
    """``username#id`` label used in log lines (never a password or a secret)."""
    if actor is None:
        return "system"
    return f"{actor.username}#{actor.id}"


def _as_int(value: Any) -> int | None:
    """Best-effort ``int`` for an id that may arrive as ``str``/``Enum``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _operator_id_of(operator: Operator | int | None) -> int | None:
    """Accept either an ``Operator`` row or a raw id and return the id."""
    if operator is None:
        return None
    if isinstance(operator, Operator):
        return _as_int(operator.id)
    return _as_int(operator)


def _clean_optional_str(value: Any) -> str | None:
    """Trim a string; map empty/blank to ``None`` (telegram fields are nullable)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Secret box (Fernet) — CONTRACTS_V2.md §7
# ---------------------------------------------------------------------------


def _secret_box_material() -> bytes:
    """Raw key material: explicit ``SECRET_BOX_KEY`` wins, else ``BACKEND_SECRET_KEY``.

    ``SECRET_BOX_KEY`` may be a full Fernet key *or* any passphrase; either way the
    value is hashed, so a user-supplied 32-byte string is never used verbatim.
    """
    from app.config import settings  # local import: avoids an import cycle

    explicit = _clean_optional_str(getattr(settings, "secret_box_key", None))
    material = explicit or _clean_optional_str(getattr(settings, "backend_secret_key", None))
    if not material:
        logger.error("neither SECRET_BOX_KEY nor BACKEND_SECRET_KEY is set; using dev fallback")
        material = _DEV_FALLBACK_SECRET
    return material.encode("utf-8")


def _derived_key() -> bytes:
    """Fernet key: ``SHA-256(material)`` re-encoded to the urlsafe-b64 form Fernet takes."""
    digest = hashlib.sha256(_secret_box_material()).digest()
    return base64.urlsafe_b64encode(digest[:_FERNET_KEY_BYTES])


def _explicit_fernet_key() -> bytes | None:
    """A literal Fernet key from ``SECRET_BOX_KEY`` (44-char urlsafe b64 of 32 bytes)."""
    from app.config import settings  # local import: avoids an import cycle

    raw = _clean_optional_str(getattr(settings, "secret_box_key", None))
    if not raw:
        return None
    candidate = raw.encode("utf-8")
    if len(candidate) != 44:
        return None
    try:
        decoded = base64.urlsafe_b64decode(candidate)
    except Exception:  # noqa: BLE001 - not a Fernet key, it is a passphrase
        return None
    return candidate if len(decoded) == _FERNET_KEY_BYTES else None


def _fernet() -> Any:
    """Build a ``Fernet`` cipher from configuration (raises when unavailable).

    Two key spellings are supported, tried in order:

    1. ``SECRET_BOX_KEY`` that is already a valid 44-character urlsafe base64 Fernet
       key — used verbatim, which is what an operator pasting
       ``Fernet.generate_key()`` expects;
    2. otherwise ``SHA-256(SECRET_BOX_KEY or BACKEND_SECRET_KEY)`` truncated to 32
       bytes and urlsafe-b64 encoded, exactly as CONTRACTS_V2.md §7 prescribes
       («Если не задан — выводится из ``BACKEND_SECRET_KEY`` через SHA-256»).
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - cryptography is in requirements
        logger.error("cryptography is not installed; Telegram secrets cannot be encrypted")
        raise RuntimeError(_MSG_NO_CIPHER) from exc
    return Fernet(_explicit_fernet_key() or _derived_key())


def encrypt_secret(value: str | None) -> str | None:
    """Encrypt ``value`` and return a Fernet token, or ``None`` for an empty input.

    An empty/blank value is stored as ``NULL`` (there is nothing to protect), which
    keeps ``UNIQUE``/``CHECK`` semantics simple. The value itself is never logged.
    """
    plain = value if isinstance(value, str) else (str(value) if value is not None else None)
    if plain is None or not plain.strip():
        return None
    token = _fernet().encrypt(plain.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(token: str | None) -> str | None:
    """Decrypt a Fernet token produced by :func:`encrypt_secret`.

    Returns ``None`` for ``NULL``/empty input. Raises ``RuntimeError`` when the token
    cannot be read (wrong or rotated key, tampered ciphertext) — a caller must never
    silently fall back to using the ciphertext as if it were the secret.
    """
    if token is None or not (isinstance(token, str) and token.strip()):
        return None
    try:
        plain = _fernet().decrypt(token.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - InvalidToken, UnicodeError, key mismatch
        logger.error("cannot decrypt a stored Telegram secret (%s)", type(exc).__name__)
        raise RuntimeError(
            "Не удалось расшифровать секрет Telegram: ключ SECRET_BOX_KEY изменился"
        ) from exc
    return plain.decode("utf-8")


def _hide(value: str | None) -> bool:
    """``True`` when a secret column holds a value (ciphertext or legacy plaintext)."""
    return bool(_clean_optional_str(value))


def mask_secrets(account: TelegramAccount) -> TelegramAccountView:
    """Return the response-safe view of ``account`` (no ciphertext, no plaintext)."""
    return TelegramAccountView(
        account,
        has_session=_hide(getattr(account, "session_string", None)),
        has_api_hash=_hide(getattr(account, "api_hash", None)),
    )


# ---------------------------------------------------------------------------
# Audit journal
# ---------------------------------------------------------------------------


async def record_audit(
    db: AsyncSession,
    *,
    actor: Operator | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
) -> None:
    """Stage one ``admin_audit_log`` row on ``db`` (commit is the caller's job).

    Staging instead of committing lets the mutation and its journal entry land in
    the same transaction: an action can never be recorded without having happened,
    nor happen without being recorded. ``detail`` must be secret-free — callers pass
    change lists, never passwords or session strings.
    """
    entry = AuditEntry(
        actor_id=_operator_id_of(actor),
        action=str(action).strip().lower(),
        target_type=_clean_optional_str(target_type),
        target_id=_as_int(target_id),
        detail=_clean_optional_str(detail),
        created_at=_now(),
    )
    db.add(entry)
    logger.info(
        "audit: actor=%s action=%s target=%s:%s",
        _actor_label(actor),
        entry.action,
        entry.target_type or "-",
        entry.target_id if entry.target_id is not None else "-",
    )


async def list_audit(
    db: AsyncSession,
    *,
    limit: int = DEFAULT_AUDIT_LIMIT,
    offset: int = 0,
    action: str | None = None,
    target_type: str | None = None,
) -> list[AuditEntry]:
    """Return audit rows, newest first (``GET /api/admin/audit``)."""
    safe_limit = max(1, min(int(limit or DEFAULT_AUDIT_LIMIT), MAX_AUDIT_LIMIT))
    safe_offset = max(0, int(offset or 0))
    stmt = select(AuditEntry)
    if action:
        stmt = stmt.where(AuditEntry.action == str(action).strip().lower())
    if target_type:
        stmt = stmt.where(AuditEntry.target_type == str(target_type).strip().lower())
    stmt = stmt.order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
    stmt = stmt.limit(safe_limit).offset(safe_offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


async def _get_operator_or_none(
    db: AsyncSession, operator_id: int, *, for_update: bool = False
) -> Operator | None:
    """Load one operator row (optionally ``FOR UPDATE``) or ``None``."""
    stmt = select(Operator).where(Operator.id == operator_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_operator(db: AsyncSession, operator_id: int) -> Operator:
    """Return the operator or raise ``404`` with a Russian message."""
    operator = await _get_operator_or_none(db, operator_id)
    if operator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_OPERATOR)
    return operator


async def _lock_active_admins(db: AsyncSession) -> list[Operator]:
    """Lock **and return** every active administrator row.

    ``SELECT ... FOR UPDATE`` on the *active-admin* set serialises concurrent role
    changes: the second transaction blocks until the first commits, then re-counts
    and sees the reduced set, so two demotions can never both observe "there are
    two admins" and both succeed.

    Row ordering is *not* an optimisation detail here: two transactions that lock the
    same rows in opposite order deadlock. PostgreSQL evaluates ``FOR UPDATE`` inside
    the ``LockRows`` node, **above** the ``Sort`` that implements ``ORDER BY``, so
    row locks are acquired in the sort's order — but a planner could fold that sort
    away (few rows, ``enable_sort=off``, a different cost estimate), silently
    reintroducing a planner-dependent acquisition order. ``ORDER BY id + 0`` cannot
    be folded into an index, so the sort — and therefore "ascending primary key" —
    is guaranteed for every planner decision.
    """
    stmt = (
        select(Operator)
        .where(Operator.role == OperatorRole.ADMIN.value)
        .order_by(Operator.id + 0)
        .with_for_update()
    )
    result = await db.execute(stmt)
    return [row for row in result.scalars().all() if is_active_operator(row)]


async def _guard_last_active_admin(
    db: AsyncSession,
    target: Operator,
    *,
    next_role: str | None = None,
    next_active: bool | None = None,
) -> None:
    """Refuse an operation that would leave the installation without an active ADMIN.

    ``next_role``/``next_active`` describe the *post-update* state of ``target``; the
    guard only does anything when ``target`` is currently an active administrator
    that is about to stop being one. Raises ``409`` — CONTRACTS_V2.md requires the
    exact wording «Нельзя изменить последнего администратора».
    """
    if not (bool(getattr(target, "is_admin", False)) and is_active_operator(target)):
        return
    will_remain_admin = (next_role if next_role is not None else _resolve_role(target)) == (
        OperatorRole.ADMIN.value
    )
    will_remain_active = next_active if next_active is not None else is_active_operator(target)
    if will_remain_admin and will_remain_active:
        return

    admins = await _lock_active_admins(db)
    remaining = [admin for admin in admins if _as_int(admin.id) != _as_int(target.id)]
    if not remaining:
        logger.warning(
            "refused to demote/deactivate the last active administrator id=%s", target.id
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_LAST_ADMIN)


async def list_operators(db: AsyncSession) -> list[Operator]:
    """Every operator account (including deactivated ones), ordered by ``username``."""
    result = await db.execute(select(Operator).order_by(Operator.username, Operator.id))
    return list(result.scalars().all())


async def create_operator(db: AsyncSession, payload: OperatorCreateRequest, *, actor: Operator) -> Operator:
    """Create an operator account and journal the action.

    ``username`` is normalised to lower-case (matching ``LoginRequest``), the
    password goes through :func:`app.security.hash_password`, and a duplicate
    username answers ``409`` instead of leaking an ``IntegrityError``.
    """
    username = str(payload.username or "").strip().lower()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Имя пользователя обязательно"
        )
    existing = (
        await db.execute(select(Operator.id).where(Operator.username == username).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_DUPLICATE_USERNAME)

    now = _now()
    operator = Operator(
        username=username,
        password_hash=hash_password(payload.password),
        display_name=str(payload.display_name or "").strip() or username,
        role=OperatorRole.coerce(payload.role) or OperatorRole.OPERATOR.value,
        is_active=bool(payload.is_active),
        max_chats=payload.max_chats,
        can_delete_messages=bool(payload.can_delete_messages),
        can_export=bool(payload.can_export),
        avatar_color=payload.avatar_color,
        # CONTRACTS_V5 §2.3: an omitted override keeps the column default so the global
        # ``app_settings.sla_target_seconds`` applies to the new account.
        sla_target_seconds=(
            int(payload.sla_target_seconds)
            if payload.sla_target_seconds is not None
            else DEFAULT_SLA_TARGET_SECONDS
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(operator)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if _is_username_conflict(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_MSG_DUPLICATE_USERNAME
            ) from None
        raise

    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_CREATE_OPERATOR,
        target_type=AUDIT_TARGET_OPERATOR,
        target_id=operator.id,
        detail=f"Создан оператор «{operator.username}» с ролью {_resolve_role(operator)}",
    )
    await db.commit()
    await db.refresh(operator)
    logger.info("operator %r created by %s", operator.username, _actor_label(actor))
    return operator


async def update_operator(
    db: AsyncSession,
    operator_id: int,
    payload: OperatorUpdateRequest,
    *,
    actor: Operator,
) -> Operator:
    """Partially update an operator account; ``username`` is ignored by design.

    A role change or an ``is_active`` flip that would leave no active administrator
    is refused with ``409`` (see :func:`_guard_last_active_admin`). A password sent
    through this endpoint is re-hashed like ``reset-password`` but journaled as
    ``update_operator`` with a masked detail line.
    """
    operator = await _get_operator_or_none(db, operator_id, for_update=True)
    if operator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_OPERATOR)

    supplied = payload.model_dump(exclude_unset=True)
    next_role = OperatorRole.coerce(supplied.get("role")) if supplied.get("role") else None
    next_active = supplied.get("is_active") if isinstance(supplied.get("is_active"), bool) else None
    await _guard_last_active_admin(db, operator, next_role=next_role, next_active=next_active)

    changes: list[str] = []
    for field in _OPERATOR_EDITABLE_FIELDS:
        if field not in supplied:
            continue
        value = supplied[field]
        if field == "role":
            if value is None:
                continue
            value = OperatorRole.coerce(value) or _resolve_role(operator)
        elif field in ("is_active", "can_delete_messages", "can_export"):
            if value is None:
                continue
            value = bool(value)
        current = getattr(operator, field, None)
        normalised_current = bool(current) if isinstance(current, bool) else current
        if normalised_current == value:
            continue
        setattr(operator, field, value)
        changes.append(f"{field}={_render_value(value)}")

    password = supplied.get("password")
    if password:
        operator.password_hash = hash_password(password)
        changes.append("password=изменён")

    if not changes:
        # Nothing to apply: return the row untouched and write no audit noise.
        await db.commit()
        await db.refresh(operator)
        logger.info("operator id=%s updated by %s: no changes", operator_id, _actor_label(actor))
        return operator

    operator.updated_at = _now()
    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_UPDATE_OPERATOR,
        target_type=AUDIT_TARGET_OPERATOR,
        target_id=operator.id,
        detail=f"Оператор «{operator.username}»: " + ", ".join(changes),
    )
    await db.commit()
    await db.refresh(operator)
    logger.info(
        "operator %r updated by %s (%s changes)",
        operator.username,
        _actor_label(actor),
        len(changes),
    )
    return operator


async def deactivate_operator(
    db: AsyncSession, operator_id: int, *, actor: Operator
) -> Operator:
    """Soft-delete an operator (``is_active = False``) and journal the action.

    The row is never removed: ``group_messages.sender_operator_id``,
    ``sla_incidents`` and ``admin_audit_log.actor_id`` all keep pointing at it.
    Repeating the call on an already inactive account is idempotent (no new audit
    row); deactivating the last active administrator raises ``409``.
    """
    operator = await _get_operator_or_none(db, operator_id, for_update=True)
    if operator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_OPERATOR)

    if not is_active_operator(operator):
        await db.commit()
        await db.refresh(operator)
        logger.info("operator id=%s is already deactivated; nothing to do", operator_id)
        return operator

    await _guard_last_active_admin(db, operator, next_active=False)
    operator.is_active = False
    operator.updated_at = _now()
    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_DEACTIVATE_OPERATOR,
        target_type=AUDIT_TARGET_OPERATOR,
        target_id=operator.id,
        detail=f"Учётная запись «{operator.username}» деактивирована",
    )
    await db.commit()
    await db.refresh(operator)
    logger.info("operator %r deactivated by %s", operator.username, _actor_label(actor))
    return operator


async def reset_password(
    db: AsyncSession,
    operator_id: int,
    new_password: str,
    *,
    actor: Operator,
) -> Operator:
    """Replace an operator's password (bcrypt) and journal the action.

    The new password is hashed before it ever touches the session and is never
    logged, never returned and never copied into the audit ``detail``.
    """
    password = str(new_password or "")
    if not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Новый пароль обязателен"
        )
    operator = await _get_operator_or_none(db, operator_id, for_update=True)
    if operator is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_OPERATOR)

    operator.password_hash = hash_password(password)
    operator.updated_at = _now()
    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_RESET_PASSWORD,
        target_type=AUDIT_TARGET_OPERATOR,
        target_id=operator.id,
        detail=f"Пароль оператора «{operator.username}» сброшен",
    )
    await db.commit()
    await db.refresh(operator)
    logger.info("password reset for operator %r by %s", operator.username, _actor_label(actor))
    return operator


def _is_username_conflict(exc: IntegrityError) -> bool:
    """``True`` when ``exc`` is the ``operators.username`` unique violation."""
    text = str(getattr(exc, "orig", exc)).lower()
    return "username" in text or "ix_operators_username" in text


def _is_account_label_conflict(exc: IntegrityError) -> bool:
    """``True`` when ``exc`` is the ``telegram_accounts.label`` unique violation."""
    text = str(getattr(exc, "orig", exc)).lower()
    return "label" in text or "uq_telegram_accounts_label" in text


def _render_value(value: Any) -> str:
    """Render a field value for the audit ``detail`` (never a secret)."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if value is None:
        return "—"
    return str(getattr(value, "value", value))


# ---------------------------------------------------------------------------
# Telegram accounts
# ---------------------------------------------------------------------------


def _account_snapshot(account: TelegramAccount) -> dict[str, Any]:
    """Secret-free snapshot of the editable account fields (for change reporting)."""
    return {field: getattr(account, field, None) for field in _ACCOUNT_EDITABLE_FIELDS}


def _describe_account_changes(before: dict[str, Any], account: TelegramAccount) -> list[str]:
    """List ``field=value`` entries that actually changed since ``before``."""
    changes: list[str] = []
    for field in _ACCOUNT_EDITABLE_FIELDS:
        old = before.get(field)
        new = getattr(account, field, None)
        if old == new:
            continue
        changes.append(f"{field}={_render_value(new)}")
    return changes


async def _get_account_or_none(
    db: AsyncSession, account_id: int, *, for_update: bool = False
) -> TelegramAccount | None:
    """Load one ``telegram_accounts`` row (optionally ``FOR UPDATE``) or ``None``."""
    stmt = select(TelegramAccount).where(TelegramAccount.id == account_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def _assert_label_free(db: AsyncSession, label: str, *, exclude_id: int | None = None) -> None:
    """Raise ``409`` when ``label`` is already used by another account."""
    stmt = select(TelegramAccount.id).where(TelegramAccount.label == label)
    if exclude_id is not None:
        stmt = stmt.where(TelegramAccount.id != exclude_id)
    taken = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_DUPLICATE_LABEL)


async def list_accounts(db: AsyncSession) -> list[TelegramAccountView]:
    """Every Telegram account with masked secrets, ordered by ``label``."""
    result = await db.execute(select(TelegramAccount).order_by(TelegramAccount.label))
    return [mask_secrets(account) for account in result.scalars().all()]


async def create_account(
    db: AsyncSession, payload: TelegramAccountCreateRequest, *, actor: Operator
) -> TelegramAccountView:
    """Store a new Telegram account, encrypting ``session_string``/``api_hash``.

    Returns the masked view: neither the plaintext nor the ciphertext ever leaves
    this function.
    """
    label = str(payload.label or "").strip()
    if not label:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Название аккаунта обязательно"
        )
    await _assert_label_free(db, label)

    now = _now()
    account = TelegramAccount(
        label=label,
        tg_user_id=payload.tg_user_id,
        tg_username=_clean_optional_str(payload.tg_username),
        phone=_clean_optional_str(payload.phone),
        session_string=encrypt_secret(payload.session_string),
        api_id=payload.api_id,
        api_hash=encrypt_secret(payload.api_hash),
        is_active=bool(payload.is_active),
        created_at=now,
        updated_at=now,
    )
    db.add(account)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if _is_account_label_conflict(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_DUPLICATE_LABEL) from None
        raise

    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_CREATE_ACCOUNT,
        target_type=AUDIT_TARGET_TELEGRAM_ACCOUNT,
        target_id=account.id,
        detail=f"Создан Telegram-аккаунт «{account.label}»",
    )
    await db.commit()
    await db.refresh(account)
    logger.info("telegram account %r created by %s", account.label, _actor_label(actor))
    return mask_secrets(account)


async def _apply_account_secrets(
    account: TelegramAccount, payload: TelegramAccountUpdateRequest
) -> list[str]:
    """Encrypt and apply the supplied secrets; return the audit change list.

    Semantics for ``PATCH`` (absent ≠ null is honoured via ``exclude_unset``):

    * field absent — unchanged;
    * explicit ``null`` — the stored secret is **cleared**;
    * non-empty string — re-encrypted (a new hash/session replaces the old one).
    """
    changes: list[str] = []
    supplied = payload.model_dump(exclude_unset=True)
    if "session_string" in supplied:
        raw = supplied.get("session_string")
        account.session_string = encrypt_secret(raw)
        changes.append("session_string=обновлён" if account.session_string else "session_string=удалён")
    if "api_hash" in supplied:
        raw = supplied.get("api_hash")
        account.api_hash = encrypt_secret(raw)
        changes.append("api_hash=обновлён" if account.api_hash else "api_hash=удалён")
    return changes


async def update_account(
    db: AsyncSession, account_id: int, payload: TelegramAccountUpdateRequest, *, actor: Operator
) -> TelegramAccountView:
    """Partially update a Telegram account; secrets are re-encrypted when supplied."""
    account = await _get_account_or_none(db, account_id, for_update=True)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_ACCOUNT)

    supplied = payload.model_dump(exclude_unset=True)
    if "label" in supplied and supplied.get("label") is not None:
        label = str(supplied["label"]).strip()
        if not label:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Название аккаунта обязательно",
            )
        await _assert_label_free(db, label, exclude_id=_as_int(account.id))

    before = _account_snapshot(account)
    for field in _ACCOUNT_EDITABLE_FIELDS:
        if field not in supplied:
            continue
        value = supplied[field]
        if field == "label":
            # Validated above; an explicit null keeps the current label.
            if value is not None:
                account.label = str(value).strip()
        elif field == "is_active":
            account.is_active = bool(value)
        elif field in ("tg_username", "phone"):
            # Nullable text columns: an explicit null clears them.
            setattr(account, field, _clean_optional_str(value))
        elif field in ("tg_user_id", "api_id"):
            # Nullable numeric columns: an explicit null clears them.
            setattr(account, field, None if value is None else int(value))

    changes = _describe_account_changes(before, account)
    changes.extend(await _apply_account_secrets(account, payload))

    if not changes:
        await db.commit()
        await db.refresh(account)
        logger.info("telegram account id=%s updated by %s: no changes", account_id, _actor_label(actor))
        return mask_secrets(account)

    account.updated_at = _now()
    try:
        await record_audit(
            db,
            actor=actor,
            action=AUDIT_ACTION_UPDATE_ACCOUNT,
            target_type=AUDIT_TARGET_TELEGRAM_ACCOUNT,
            target_id=account.id,
            detail=f"Аккаунт «{account.label}»: " + ", ".join(changes),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_account_label_conflict(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_DUPLICATE_LABEL) from None
        raise
    await db.refresh(account)
    logger.info(
        "telegram account %r updated by %s (%s changes)",
        account.label,
        _actor_label(actor),
        len(changes),
    )
    return mask_secrets(account)


async def delete_account(db: AsyncSession, account_id: int, *, actor: Operator) -> bool:
    """Hard-delete a Telegram account row and journal it. ``True`` when removed.

    Unlike operators, accounts carry no history that must survive (messages and audit
    rows reference operators, not accounts), so a real ``DELETE`` is correct here —
    CONTRACTS_V2.md §3.3 lists ``DELETE /api/admin/accounts/{id}`` without any soft
    semantics. A missing row raises ``404`` so the UI can refresh its list.
    """
    account = await _get_account_or_none(db, account_id, for_update=True)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND_ACCOUNT)

    label = account.label
    await db.delete(account)
    await record_audit(
        db,
        actor=actor,
        action=AUDIT_ACTION_DELETE_ACCOUNT,
        target_type=AUDIT_TARGET_TELEGRAM_ACCOUNT,
        target_id=account_id,
        detail=f"Удалён Telegram-аккаунт «{label}»",
    )
    await db.commit()
    logger.info("telegram account %r deleted by %s", label, _actor_label(actor))
    return True


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def count_operators(db: AsyncSession) -> int:
    """Total number of operator rows (used by the admin dashboard)."""
    return int((await db.execute(select(func.count(Operator.id)))).scalar_one() or 0)


async def count_accounts(db: AsyncSession) -> int:
    """Total number of Telegram account rows."""
    return int((await db.execute(select(func.count(TelegramAccount.id)))).scalar_one() or 0)

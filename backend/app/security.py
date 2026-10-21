"""Password hashing, JWT issuing/decoding and FastAPI auth dependencies.

Security posture
----------------
* Passwords are hashed with **bcrypt** through ``passlib``. passlib 1.7.4 was
  written before bcrypt >= 4.1 and (a) probes ``bcrypt.__about__.__version__``
  which no longer exists, and (b) on bcrypt >= 5 lets the backend raise
  ``ValueError`` for inputs longer than 72 bytes instead of truncating. Both are
  neutralised here: the version probe error is silenced and the 72-byte cap is
  applied **before** the backend, so hashing is deterministic on bcrypt 4.0.1
  (the pinned version) and on any newer release.
* JWTs come from ``python-jose`` (HS256, ``BACKEND_SECRET_KEY``). Every failure
  mode — bad signature, expiry, wrong ``typ``, malformed claims — is converted
  into :class:`InvalidTokenError` so routers can answer a uniform 401.
* Tokens are never logged. Username lookups are exact-match (no ``LIKE``), and
  the "user missing" and "wrong password" paths deliberately produce the same
  error message to avoid account enumeration.
* ``app.config`` / ``app.database`` are imported **inside** the dependency bodies
  to keep this module importable before those modules finish loading.
"""

from __future__ import annotations

import logging
import warnings
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.enums import OperatorRole
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime here
    from app.models import Operator
logger = logging.getLogger("app.security")

__all__ = [
    "ADMIN_REQUIRED_DETAIL",
    "ADMIN_SCOPE",
    "ALGORITHM",
    "BCRYPT_MAX_PASSWORD_BYTES",
    "AdminTokenPayload",
    "InvalidTokenError",
    "OPERATOR_SCOPE",
    "PRIVILEGED_REQUIRED_DETAIL",
    "TokenPayload",
    "bearer",
    "create_access_token",
    "create_admin_token",
    "decode_access_token",
    "decode_admin_token",
    "get_current_operator",
    "hash_password",
    "is_active_operator",
    "require_admin",
    "require_privileged",
    "require_supervisor",
    "verify_password",
]

#: HS256 is symmetric and sufficient here: the backend is the only issuer and
#: the only verifier (the worker never validates user tokens).
ALGORITHM: Final[str] = "HS256"

#: bcrypt only consumes the first 72 bytes of a password.
BCRYPT_MAX_PASSWORD_BYTES: Final[int] = 72

#: ``scope`` claim of a regular desk token (``/api/auth/login``).
OPERATOR_SCOPE: Final[str] = "operator"

#: ``scope`` claim of an administrator token (``/api/admin/login``, CONTRACTS_V2.md §3.3).
#: Admin tokens are deliberately **not** accepted by the operator endpoints and vice
#: versa: the admin panel is a separate login form, so a compromised desk session can
#: never reach the operator/account management API.
ADMIN_SCOPE: Final[str] = "admin"

#: Default admin-session lifetime when ``settings`` exposes none.
_DEFAULT_ADMIN_TTL_MINUTES: Final[int] = 480

#: Message of every 403 raised by :func:`require_admin` / :func:`require_privileged`.
ADMIN_REQUIRED_DETAIL: Final[str] = "Требуются права администратора"
PRIVILEGED_REQUIRED_DETAIL: Final[str] = "Требуются права администратора или супервизора"

#: Fallback signing key used **only** when config has no secret at all, so that a
#: half-configured dev box cannot mint tokens with an empty key. Production must
#: set ``BACKEND_SECRET_KEY`` (config.py documents this in .env.example).
_DEV_FALLBACK_SECRET: Final[str] = "tg-desk-dev-only-insecure-secret-change-me"

# passlib emits this on import against bcrypt >= 4.1; we compensate below.
warnings.filterwarnings("ignore", message=r".*error reading bcrypt version.*")
warnings.filterwarnings("ignore", category=UserWarning, module=r"passlib.*")


def _install_bcrypt_compat_shim() -> None:
    """Make passlib 1.7.4 usable with bcrypt >= 4.1 / >= 5.

    Two independent breakages are neutralised:

    1. ``bcrypt.__about__.__version__`` was removed in bcrypt 4.1, so passlib logs
       a noisy (trapped) warning on backend load. Harmless but confusing.
    2. passlib's one-time backend self-test (:func:`detect_wrap_bug`) hashes a
       **255-byte** probe secret to check for the historical BSD wraparound bug.
       bcrypt >= 5.0 raises ``ValueError`` for secrets longer than 72 bytes
       instead of truncating, so that self-test now aborts *every* hash call.

    For (2) the self-test result is irrelevant to us: passlib only uses it to set a
    cosmetic workaround flag for ``$2a$`` hashes, and we always generate ``$2b$``.
    The self-test is therefore skipped on failure and the workaround flag left at
    its safe default. Real hashing/verification continues through the normal
    passlib code path, byte-capped by :func:`_truncate_password`, which keeps
    stored hashes fully interoperable with a plain ``bcrypt`` install (4.0.1 is
    the version pinned in ``requirements.txt``).

    Idempotent and safe if passlib internals change: any failure leaves passlib
    untouched, in which case bcrypt 4.0.1 (the pinned version) works as-is.
    """
    try:
        from passlib.handlers import bcrypt as passlib_bcrypt

        base = passlib_bcrypt._BcryptCommon
        original_finalize = base.__dict__["_finalize_backend_mixin"].__func__
    except (ImportError, AttributeError, KeyError, TypeError):
        return

    if getattr(base, "_tg_desk_compat", False):
        return

    def _safe_finalize(mixin_cls: Any, backend: str, dryrun: bool) -> bool:
        try:
            return original_finalize(mixin_cls, backend, dryrun)
        except Exception as exc:  # noqa: BLE001 - probe failure must not break hashing
            logger.debug(
                "passlib bcrypt self-test skipped (%s); assuming $2b$ semantics",
                type(exc).__name__,
            )
            mixin_cls._workrounds_initialized = True
            mixin_cls._lacks_20_support = True
            return True

    base._finalize_backend_mixin = classmethod(_safe_finalize)  # type: ignore[assignment]
    base._tg_desk_compat = True  # type: ignore[attr-defined]


_install_bcrypt_compat_shim()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class InvalidTokenError(Exception):
    """Raised when a bearer token is missing, malformed, forged or expired."""


class TokenPayload(BaseModel):
    """Decoded and validated access-token claims."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(gt=0)
    username: str = Field(min_length=1, max_length=64)
    role: str = OperatorRole.OPERATOR.value
    #: ``"operator"`` for desk tokens, ``"admin"`` for admin-panel tokens. Defaults
    #: to ``"operator"`` so tokens minted before the claim existed keep working.
    scope: str = OPERATOR_SCOPE

    @property
    def is_supervisor(self) -> bool:
        """``True`` when the token belongs to a supervisor."""
        return self.role == OperatorRole.SUPERVISOR.value

    @property
    def is_admin(self) -> bool:
        """``True`` when the token carries the administrator role."""
        return self.role == OperatorRole.ADMIN.value

    @property
    def is_admin_scope(self) -> bool:
        """``True`` when the token was issued by ``POST /api/admin/login``."""
        return self.scope == ADMIN_SCOPE


class AdminTokenPayload(TokenPayload):
    """Claims of an admin-scope token: :class:`TokenPayload` narrowed to ``ADMIN``.

    Used by :func:`require_admin` / :func:`require_privileged`, which additionally
    re-read the operator row (role and ``is_active`` are re-checked on every
    request, so a demoted or deactivated account loses access immediately).
    """

    scope: str = ADMIN_SCOPE


def _db_dependency() -> Any:
    """Return ``app.database.get_db`` resolved **at request time**.

    Resolving the dependency lazily keeps ``app.database`` (and therefore
    ``app.config``) out of this module's import graph, so ``security`` can be
    imported from any layer without risking a cycle.
    """
    from app.database import get_db

    return Depends(get_db)


#: ``Depends(get_db)`` resolved lazily; see :func:`_db_dependency`.
DBDependency: Any = _db_dependency()

#: ``HTTPBearer`` instead of ``OAuth2PasswordBearer``: the API is JSON-based and
#: the frontend only ever sends a bearer header (CONTRACTS.md §3), so no form
#: endpoint or ``/token`` URL is needed. ``auto_error=False`` lets us produce the
#: exact ``{"detail": "Not authenticated"}`` body ourselves.
bearer = HTTPBearer(auto_error=False, description="JWT access token from /api/auth/login")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def _truncate_password(password: str) -> str:
    """Truncate ``password`` to bcrypt's 72-**byte** limit on UTF-8 boundaries.

    ``str[:72]`` would split a multi-byte character and raise; encoding first and
    then slicing raw bytes could leave a broken sequence in the middle, so the
    prefix is trimmed until it decodes cleanly.
    """
    encoded = password.encode("utf-8")
    if len(encoded) <= BCRYPT_MAX_PASSWORD_BYTES:
        return password
    return encoded[:BCRYPT_MAX_PASSWORD_BYTES].decode("utf-8", errors="ignore")


def hash_password(password: str) -> str:
    """Return a bcrypt hash for ``password``.

    Raises ``ValueError`` for an empty password; every other input is handled
    (including multi-megabyte strings, which are truncated to 72 bytes).
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    return pwd_context.hash(_truncate_password(password))


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` when ``plain`` matches ``hashed``.

    Never raises: any passlib/backing-library exception (unknown scheme, corrupt
    hash, ValueError from newer bcrypt) is treated as a failed verification.
    """
    if not plain or not hashed or not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    try:
        return bool(pwd_context.verify(_truncate_password(plain), hashed))
    except Exception as exc:  # noqa: BLE001 - verification must never explode
        logger.warning("password verification failed: %s", type(exc).__name__)
        return False


def password_needs_rehash(hashed: str) -> bool:
    """Return ``True`` when ``hashed`` uses an outdated scheme/parameters."""
    try:
        return bool(pwd_context.needs_update(hashed))
    except Exception:  # noqa: BLE001 - unparsable hash should simply be re-hashed
        return True


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def _secret_key() -> str:
    """Return the configured signing key (read lazily; never logged)."""
    from app.config import settings  # local import: avoids an import cycle

    secret = getattr(settings, "backend_secret_key", None) or ""
    secret = str(secret).strip()
    if not secret:
        logger.error("BACKEND_SECRET_KEY is not configured; using the dev fallback key")
        return _DEV_FALLBACK_SECRET
    return secret


def _resolve_expiry_minutes(expires_minutes: int | None) -> int:
    """Return the token lifetime in minutes (explicit value wins over config).

    A **negative** explicit value is honoured verbatim: it produces an
    already-expired token, which is exactly what tests and the ``exp`` invariant
    check need. ``0`` is rejected downstream by the claim validator, and only the
    *config* value is clamped to a sane positive default so a mis-typed
    environment variable can never mint immortal tokens.
    """
    if expires_minutes is not None:
        return int(expires_minutes)
    from app.config import settings  # local import: avoids an import cycle

    configured = getattr(settings, "backend_access_token_expire_minutes", None)
    try:
        return max(int(configured), 1) if configured is not None else 720
    except (TypeError, ValueError):  # pragma: no cover - config is validated upstream
        return 720


def create_access_token(
    subject: int | str,
    *,
    username: str,
    role: str,
    expires_minutes: int | None = None,
) -> str:
    """Create a signed HS256 access token.

    Claims: ``sub`` (operator id as a string, per RFC 7519), ``username``,
    ``role``, ``scope="operator"``, ``iat``, ``exp``, ``typ="access"``.
    """
    now = utcnow()
    lifetime = timedelta(minutes=_resolve_expiry_minutes(expires_minutes))
    claims: dict[str, Any] = {
        "sub": str(subject),
        "username": username,
        "role": OperatorRole.coerce(role) or OperatorRole.OPERATOR.value,
        "scope": OPERATOR_SCOPE,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "typ": "access",
    }
    return jwt.encode(claims, _secret_key(), algorithm=ALGORITHM)


def _resolve_admin_expiry_minutes() -> int:
    """Admin token lifetime (minutes) from ``settings.admin_session_ttl_minutes``.

    CONTRACTS_V2.md §7 owns the field; ``app/config.py`` may not declare it yet, in
    which case :data:`_DEFAULT_ADMIN_TTL_MINUTES` (the contract default, 480) is
    used. A non-positive/mangled value also falls back to that default so a
    mis-typed environment variable can never mint immortal admin tokens.
    """
    from app.config import settings  # local import: avoids an import cycle

    configured = getattr(settings, "admin_session_ttl_minutes", None)
    try:
        minutes = int(configured) if configured is not None else _DEFAULT_ADMIN_TTL_MINUTES
    except (TypeError, ValueError):  # pragma: no cover - config is validated upstream
        minutes = _DEFAULT_ADMIN_TTL_MINUTES
    return minutes if minutes > 0 else _DEFAULT_ADMIN_TTL_MINUTES


def create_admin_token(operator: Operator) -> tuple[str, datetime]:
    """Mint an admin-scope token for ``operator`` and return ``(token, expires_at)``.

    The token carries ``scope="admin"`` and the operator's ``role``, so the admin
    endpoints can distinguish it from a desk token (§3.3: "отдельный токен с
    ``scope: "admin"``"). ``expires_at`` is the timezone-aware UTC expiry that
    ``POST /api/admin/login`` returns as ``expires_at``.

    The caller is responsible for verifying the operator is an active ADMIN and
    for having checked the password — this function only signs claims.
    """
    now = utcnow()
    expires_at = now + timedelta(minutes=_resolve_admin_expiry_minutes())
    claims: dict[str, Any] = {
        "sub": str(operator.id),
        "username": str(operator.username),
        "role": OperatorRole.coerce(str(operator.role)) or OperatorRole.OPERATOR.value,
        "scope": ADMIN_SCOPE,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "typ": "access",
    }
    token = jwt.encode(claims, _secret_key(), algorithm=ALGORITHM)
    return token, expires_at


def decode_access_token(token: str) -> TokenPayload:
    """Validate ``token`` and return its claims as :class:`TokenPayload`.

    Raises :class:`InvalidTokenError` for any failure: bad signature, expired or
    not-yet-valid token, wrong algorithm/``typ``, non-numeric ``sub``, or claims
    that fail validation. The raised message is intentionally generic so it can
    be surfaced to clients without leaking why the token was rejected.

    A token minted before the ``scope`` claim existed decodes as
    ``scope="operator"``; admin-scope tokens are validated by
    :func:`decode_admin_token`.
    """
    if not token or not isinstance(token, str):
        raise InvalidTokenError("missing token")
    try:
        claims = jwt.decode(
            token,
            _secret_key(),
            algorithms=[ALGORITHM],
            options={"require_sub": True, "require_exp": True, "verify_aud": False},
        )
    except JWTError as exc:
        raise InvalidTokenError("invalid or expired token") from exc
    except Exception as exc:  # noqa: BLE001 - jose can raise non-JWTError on odd input
        raise InvalidTokenError("invalid token") from exc

    if claims.get("typ") != "access":
        raise InvalidTokenError("invalid token type")
    raw_subject = claims.get("sub")
    try:
        operator_id = int(raw_subject)
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("invalid token subject") from exc
    raw_scope = claims.get("scope")
    scope = str(raw_scope).strip() if raw_scope is not None else OPERATOR_SCOPE
    try:
        return TokenPayload(
            id=operator_id,
            username=str(claims.get("username") or ""),
            role=str(claims.get("role") or OperatorRole.OPERATOR.value),
            scope=scope or OPERATOR_SCOPE,
        )
    except ValidationError as exc:
        raise InvalidTokenError("invalid token claims") from exc


def decode_admin_token(token: str) -> AdminTokenPayload:
    """Validate an **admin-scope** token and return its claims.

    Raises :class:`InvalidTokenError` when the signature/expiry is wrong, when the
    signature is valid but the token was issued by ``/api/auth/login``
    (``scope != "admin"``), or when it lacks an ADMIN/SUPERVISOR role claim. The admin
    router additionally re-checks the operator row (see :func:`require_admin`).

    ``SUPERVISOR`` is accepted because CONTRACTS_V2 §3.3 grants that role read-only
    access to the operator roster, which lives on this router. The role gate itself
    stays where it belongs — :func:`require_admin` for every mutation and
    :func:`require_privileged` for the read-only routes — so a supervisor token can
    never reach a write endpoint.
    """
    payload = decode_access_token(token)
    if payload.scope != ADMIN_SCOPE:
        raise InvalidTokenError("token is not an admin token")
    if payload.role not in (OperatorRole.ADMIN.value, OperatorRole.SUPERVISOR.value):
        raise InvalidTokenError("token does not carry a privileged role")
    return AdminTokenPayload(
        id=payload.id, username=payload.username, role=payload.role, scope=payload.scope
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Any = DBDependency,
) -> Any:
    """Resolve the authenticated :class:`~app.models.Operator` for this request.

    Returns 401 when the ``Authorization`` header is absent, non-bearer or carries
    an invalid/expired token, and 403 when the token is valid but the operator row
    no longer exists (deleted or revoked).
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    if (credentials.scheme or "").lower() != "bearer":
        raise _UNAUTHENTICATED
    try:
        payload = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _UNAUTHENTICATED from None

    # Local import: keeps ``app.models`` (and with it SQLAlchemy mapping setup)
    # off this module's import-time graph.
    from app.models import Operator

    result = await db.execute(select(Operator).where(Operator.id == payload.id).limit(1))
    operator = result.scalar_one_or_none()
    if operator is None:
        logger.info("token references a missing operator id=%s", payload.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator no longer exists",
        )
    return operator


async def require_supervisor(operator: Any = Depends(get_current_operator)) -> Any:
    """Allow only supervisors through (403 otherwise)."""
    role = getattr(operator, "role", None)
    is_supervisor = getattr(operator, "is_supervisor", None)
    allowed = bool(is_supervisor) if isinstance(is_supervisor, bool) else (
        role == OperatorRole.SUPERVISOR.value
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Supervisor privileges required",
        )
    return operator


# ---------------------------------------------------------------------------
# Admin-panel dependencies (CONTRACTS_V2.md §3.3)
# ---------------------------------------------------------------------------


def is_active_operator(operator: Any) -> bool:
    """Return ``True`` when ``operator`` may authenticate (``is_active`` not disabled).

    The ``operators.is_active`` column is owned by the models agent
    (CONTRACTS_V2.md §2.3); until it exists every account counts as active, which
    is exactly the contract default. An ``is_active`` that *is* present is always
    honoured, so deactivating an account takes effect immediately.
    """
    flag = getattr(operator, "is_active", None)
    if isinstance(flag, bool):
        return flag
    return True


async def _load_admin_operator(db: Any, credentials: HTTPAuthorizationCredentials | None) -> Any:
    """Resolve the operator behind an admin-scope bearer token.

    Raises 401 for a missing/malformed/expired/operator-scope token (the admin
    panel needs its own login form) and 403 — always with
    :data:`ADMIN_REQUIRED_DETAIL` — when the token is valid but the account is no
    longer an active administrator. Re-reading the row makes revocation instant:
    a demoted or deactivated admin cannot keep using an already-issued token.
    """
    forbidden = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_REQUIRED_DETAIL)
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    if (credentials.scheme or "").lower() != "bearer":
        raise _UNAUTHENTICATED
    try:
        payload = decode_admin_token(credentials.credentials)
    except InvalidTokenError:
        raise _UNAUTHENTICATED from None

    from app.models import Operator  # local import: keeps the import graph acyclic

    result = await db.execute(select(Operator).where(Operator.id == payload.id).limit(1))
    operator = result.scalar_one_or_none()
    if operator is None:
        logger.info("admin token references a missing operator id=%s", payload.id)
        raise forbidden
    if not is_active_operator(operator):
        logger.info("admin token used by a deactivated operator id=%s", payload.id)
        raise forbidden
    return operator, payload


async def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Any = DBDependency,
) -> Any:
    """Allow only active administrators holding an **admin-scope** token.

    Checks, in order: the token signature/expiry, ``scope == "admin"``,
    ``operator.is_admin`` and ``operator.is_active``. Every mismatch — except a
    missing/invalid token, which is a 401 — answers
    ``403 {"detail": "Требуются права администратора"}``.
    """
    operator, _payload = await _load_admin_operator(db, credentials)
    if not bool(getattr(operator, "is_admin", False)):
        logger.info("admin endpoint refused for operator id=%s", getattr(operator, "id", "?"))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ADMIN_REQUIRED_DETAIL)
    return operator


async def require_privileged(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Any = DBDependency,
) -> Any:
    """Allow ``ADMIN`` **or** ``SUPERVISOR`` holding an admin-scope token.

    Privileged-but-not-admin accounts may read what §3.3 grants supervisors (the
    operator roster); every mutating admin endpoint stays on :func:`require_admin`.
    """
    operator, _payload = await _load_admin_operator(db, credentials)
    role = str(getattr(operator, "role", "") or "")
    if role not in (OperatorRole.ADMIN.value, OperatorRole.SUPERVISOR.value):
        logger.info(
            "privileged endpoint refused for operator id=%s", getattr(operator, "id", "?")
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=PRIVILEGED_REQUIRED_DETAIL
        )
    return operator

"""Application configuration.

Load precedence (highest wins):

1. Real process environment variables.
2. `<repo-root>/.env` — resolved relative to this file, so it works from any CWD.
3. `<repo-root>/backend/.env` — per-service override / legacy fallback.
4. The defaults declared on :class:`Settings`, which mirror ``.env.example`` so the whole
   stack boots with zero editing (see CONTRACTS.md §1).

The module never raises when no ``.env`` file exists: every field has a default.

Downstream agents must import ``settings`` from here — never rebuild a URL or read
``os.environ`` directly.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
from pathlib import Path
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# backend/app/config.py -> parents[0]=app, [1]=backend, [2]=<repo-root>
BACKEND_DIR: Path = Path(__file__).resolve().parents[1]
ROOT_DIR: Path = Path(__file__).resolve().parents[2]

#: Replacement used for passwords in log output.
REDACTED_PASSWORD: str = "***"

#: Default media tree, relative to :data:`ROOT_DIR` (CONTRACTS_V2.md §7).
DEFAULT_MEDIA_SUBDIR: Path = Path("storage/media")


class Settings(BaseSettings):
    """Typed view over the repo-root ``.env`` (CONTRACTS.md §1)."""

    model_config = SettingsConfigDict(
        # Later files win over earlier ones; real env vars and init kwargs win over all files.
        env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- PostgreSQL
    postgres_user: str = "tgcrm"
    postgres_password: str = "tgcrm_pass"
    postgres_db: str = "tgcrm"
    postgres_host: str = "localhost"
    postgres_port: int = 5433

    # --------------------------------------------------------------------- Redis
    redis_url: str = "redis://localhost:6380/0"

    # ------------------------------------------------------------------- Backend
    backend_secret_key: str = "change-me-in-production-please-use-a-long-random-string"
    backend_access_token_expire_minutes: int = 720
    backend_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    backend_sla_seconds: int = 300
    backend_ws_heartbeat_timeout_seconds: int = 45
    backend_ws_tick_seconds: int = 2
    backend_supervisor_username: str = "admin"
    backend_supervisor_password: str = "admin123"
    backend_seed_operator_password: str = "operator123"
    app_env: str = "dev"

    # ------------------------------------------------------- Admin adapters (mock)
    admin_one_base_url: str = "https://admin-one.internal.example/api"
    admin_one_api_key: str = "mock-admin-one-key"
    admin_two_base_url: str = "https://admin-two.internal.example/api"
    admin_two_api_key: str = "mock-admin-two-key"

    # ------------------------------------------------------------ Telethon worker
    tg_api_id: int = 123456
    tg_api_hash: str = "0123456789abcdef0123456789abcdef"
    tg_session_string: str = ""
    tg_string_session: str = ""
    tg_session_file: str = "./telethon_worker/tg_desk.session"
    # Comma separated list of TG chat ids (or @usernames) monitored by the worker.
    tg_monitored_chat_ids: str = ""
    telegram_chat_id: str = ""
    telegram_admin_chat_id: str = ""
    telegram_admin_chat_ids: str = ""

    # ---------------------------------------------------------------------- Media
    #: Root of the attachment tree (CONTRACTS_V2.md §7). Relative values are
    #: resolved against ``ROOT_DIR`` — never against CWD — by
    #: :meth:`_resolve_media_root`, so the API, the worker and the tests all point
    #: at the same directory no matter where they were started from.
    media_root: Path = Path("storage/media")
    #: Hard cap for an operator upload (25 MiB, Telegram's own bot limit).
    max_upload_bytes: int = 25 * 1024 * 1024
    #: Fernet key used to encrypt ``telegram_accounts`` secrets. ``None`` means
    #: "derive it from ``backend_secret_key``" (see :attr:`fern_key`).
    secret_box_key: str | None = None

    # --------------------------------------------------------------- Validators
    @field_validator("media_root", mode="before")
    @classmethod
    def _resolve_media_root(cls, value: str | Path | None) -> Path:
        """Make ``MEDIA_ROOT`` an absolute path anchored at the repository root."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return ROOT_DIR / DEFAULT_MEDIA_SUBDIR
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        return ROOT_DIR / candidate

    # --------------------------------------------------------------- Derived data
    @property
    def database_url(self) -> str:
        """Async DSN used by SQLAlchemy/asyncpg (CONTRACTS.md §1)."""
        return (
            f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}"
            f":{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}"
            f"/{quote(self.postgres_db, safe='')}"
        )

    @property
    def sync_database_url(self) -> str:
        """Blocking DSN (Alembic / scripts) — no async driver, no psycopg dependency."""
        return (
            f"postgresql://{quote(self.postgres_user, safe='')}"
            f":{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}"
            f"/{quote(self.postgres_db, safe='')}"
        )

    @property
    def database_url_safe(self) -> str:
        """DSN with the password redacted — the only form that may be logged."""
        return (
            f"postgresql+asyncpg://{quote(self.postgres_user, safe='')}:***"
            f"@{self.postgres_host}:{self.postgres_port}"
            f"/{quote(self.postgres_db, safe='')}"
        )

    @property
    def redis_url_safe(self) -> str:
        """Redis URL with any embedded password redacted, for log output."""
        return redact_url_password(self.redis_url)

    @property
    def cors_origins(self) -> list[str]:
        """``BACKEND_CORS_ORIGINS`` string -> de-duplicated list, order preserved."""
        seen: set[str] = set()
        origins: list[str] = []
        for raw in self.backend_cors_origins.replace(";", ",").split(","):
            origin = raw.strip().rstrip("/")
            if origin and origin not in seen:
                seen.add(origin)
                origins.append(origin)
        return origins

    @property
    def monitored_chat_ids(self) -> list[str]:
        """``TG_MONITORED_CHAT_IDS`` string -> list of ids/usernames."""
        return [part.strip() for part in self.tg_monitored_chat_ids.split(",") if part.strip()]

    @property
    def admin_chat_ids(self) -> list[str]:
        """Union of ``TELEGRAM_ADMIN_CHAT_IDS`` and ``TELEGRAM_ADMIN_CHAT_ID`` (de-duplicated)."""
        ids: list[str] = [
            part.strip() for part in self.telegram_admin_chat_ids.split(",") if part.strip()
        ]
        single = self.telegram_admin_chat_id.strip()
        if single and single not in ids:
            ids.append(single)
        return ids

    @property
    def tg_session(self) -> str:
        """Telethon StringSession value: ``TG_SESSION_STRING`` preferred, ``TG_STRING_SESSION`` fallback."""
        return self.tg_session_string or self.tg_string_session

    @property
    def access_token_expire_seconds(self) -> int:
        """Token lifetime in seconds (what ``/api/auth/login`` returns as ``expires_in``)."""
        return self.backend_access_token_expire_minutes * 60

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    @property
    def fern_key(self) -> str:
        """URL-safe base64 Fernet key for the secret box (CONTRACTS_V2.md §7).

        When ``SECRET_BOX_KEY`` is set it is returned as-is (it must already be a
        valid 32-byte url-safe base64 key); otherwise the key is derived from
        ``BACKEND_SECRET_KEY`` with ``base64.urlsafe_b64encode(sha256(secret).digest())``,
        which yields exactly the shape Fernet expects.
        """
        configured = (self.secret_box_key or "").strip()
        if configured:
            return configured
        digest = hashlib.sha256(self.backend_secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii")


def redact_url_password(url: str) -> str:
    """Replace the ``user:password@`` fragment of a URL with ``user:***@``."""
    scheme_sep = "://"
    if scheme_sep not in url:
        return url
    scheme, _, rest = url.partition(scheme_sep)
    authority, at, tail = rest.rpartition("@")
    if not at:
        return url
    user, sep, password = authority.partition(":")
    if not sep or not password:
        return url
    return f"{scheme}{scheme_sep}{user}:{REDACTED_PASSWORD}{at}{tail}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings instance (also available as the ``settings`` singleton)."""
    return Settings()


settings: Settings = get_settings()

__all__ = [
    "BACKEND_DIR",
    "DEFAULT_MEDIA_SUBDIR",
    "ROOT_DIR",
    "Settings",
    "get_settings",
    "redact_url_password",
    "settings",
]

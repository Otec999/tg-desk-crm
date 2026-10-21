"""Contract smoke tests — pure unit tests, no PostgreSQL and no Redis required.

Run from the repo root::

    python -m pytest -q backend/tests

The module-level ``importorskip`` keeps the suite green on machines where the backend
dependencies were never installed.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pydantic_settings")
pytest.importorskip("sqlalchemy")
pytest.importorskip("redis")
pytest.importorskip("orjson")

from app.config import Settings, get_settings, settings  # noqa: E402
from app.ws_manager import ConnectionManager, envelope, manager  # noqa: E402

# ============================================================ config: defaults & derived URLs


def test_settings_defaults_match_contract() -> None:
    """Default SLA window is 300s (CONTRACTS.md §1)."""
    assert settings.backend_sla_seconds == 300
    assert settings.backend_ws_heartbeat_timeout_seconds == 45
    assert settings.backend_ws_tick_seconds == 2
    assert settings.backend_access_token_expire_minutes == 720
    assert settings.app_env == "dev"


def test_database_url_is_asyncpg_on_port_5433() -> None:
    """DATABASE_URL is derived with asyncpg and the host-mapped port 5433."""
    url = settings.database_url
    assert url.startswith("postgresql+asyncpg://")
    assert ":5433/" in url
    assert url.endswith(f"/{settings.postgres_db}")


def test_sync_database_url_has_no_async_driver() -> None:
    assert settings.sync_database_url.startswith("postgresql://")
    assert "asyncpg" not in settings.sync_database_url


def test_credentials_are_url_encoded() -> None:
    """Passwords containing reserved characters must be percent-encoded, never raw."""
    tricky = Settings(postgres_user="user name", postgres_password="p@ss:w/rd?x", postgres_db="db")
    url = tricky.database_url
    assert "p@ss:w/rd?x" not in url
    assert "p%40ss%3Aw%2Frd%3Fx" in url
    assert "user%20name" in url


def test_password_is_redacted_for_logging() -> None:
    from app.config import redact_url_password

    redacted = redact_url_password("redis://user:secret@localhost:6380/0")
    assert "secret" not in redacted
    assert redacted == "redis://user:***@localhost:6380/0"


def test_cors_origins_parsed_into_list() -> None:
    configured = Settings(backend_cors_origins="http://localhost:5173, http://127.0.0.1:5173 ,http://localhost:5173")
    assert configured.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_get_settings_is_cached_singleton() -> None:
    assert get_settings() is get_settings()


def test_settings_boot_without_env_file() -> None:
    """All fields have defaults, so a missing .env must not raise."""
    assert Settings().postgres_host
    assert Settings().redis_url.startswith("redis://")


# =========================================================================== websocket envelope


def test_envelope_shape_and_utc_z_timestamp() -> None:
    """Envelopes are exactly ``{"type","ts","data"}`` with a Z-suffixed UTC ts (§4)."""
    env = envelope("X", {"a": 1})
    assert set(env) == {"type", "ts", "data"}
    assert env["type"] == "X"
    assert env["data"] == {"a": 1}
    assert env["ts"].endswith("Z")
    assert "T" in env["ts"]


def test_envelope_does_not_share_mutable_data() -> None:
    first = envelope("A")
    second = envelope("B")
    assert first["data"] == {}
    first["data"]["mutated"] = True
    assert second["data"] == {}


async def _manager_starts_empty() -> None:
    """A fresh manager exposes the documented introspection API."""
    assert manager.connection_count() == 0
    assert manager.online_operator_ids() == []
    fresh = ConnectionManager()
    assert fresh.connection_count() == 0


async def _manager_heartbeat_and_stale_sweep() -> None:
    fresh = ConnectionManager()
    fresh.touch_heartbeat(42)
    assert fresh.last_heartbeat_age(42) is not None
    # No sockets are registered for operator 42, so nothing can time out.
    assert await fresh.sweep_stale(timeout_seconds=0.0) == []


async def _broadcast_without_connections() -> None:
    fresh = ConnectionManager()
    assert await fresh.broadcast(envelope("NEW_MESSAGE", {"id": 1})) == 0
    assert await fresh.send_to_operator(7, envelope("PING")) == 0


def test_manager_state_and_broadcast_are_async_safe() -> None:
    """Exercised through asyncio.run so the suite needs no pytest-asyncio plugin."""
    asyncio.run(_manager_starts_empty())
    asyncio.run(_manager_heartbeat_and_stale_sweep())
    asyncio.run(_broadcast_without_connections())


# ================================================================================= redis facade


def test_redis_bus_exports_required_api() -> None:
    """Module surface other agents rely on (no I/O performed here)."""
    from app import redis_bus

    for name in (
        "publish",
        "pubsub",
        "get_redis",
        "close_redis",
        "RedisPubSub",
        "set_typing_lock",
        "get_typing_lock",
        "clear_typing_lock",
    ):
        assert hasattr(redis_bus, name), f"redis_bus.{name} missing"
    assert redis_bus.TYPING_LOCK_PREFIX == "typing_lock"
    assert redis_bus.typing_lock_key(12) == "typing_lock:12"


def test_redis_payload_encoding_uses_orjson() -> None:
    from app.redis_bus import encode_payload

    assert encode_payload({"a": 1, "b": "x"}) == '{"a":1,"b":"x"}'

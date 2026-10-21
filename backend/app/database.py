"""Async SQLAlchemy 2.0 engine, session factory and FastAPI DB dependency.

Design notes
------------
* ``Base`` lives in ``app/models.py`` (owned by the models agent) and is imported **lazily**
  inside :func:`init_models` so importing this module never triggers model imports and never
  creates an import cycle (``models`` -> ``database`` is fine, ``database`` -> ``models`` at
  module import time is not).
* Nothing here opens a connection at import time: the engine is lazy, so the API can boot and
  serve ``/health`` with ``db: false`` while PostgreSQL is unreachable.
* JSON columns are serialised with orjson for consistency with the WebSocket layer.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

logger = logging.getLogger(__name__)

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
    json_serializer=orjson.dumps,
    json_deserializer=orjson.loads,
    future=True,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, rolled back on error and always closed."""
    session: AsyncSession = SessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory for background tasks/watchers (they should not share request sessions)."""
    return SessionLocal


async def init_models() -> None:
    """Create every table declared in ``app.models`` (``Base.metadata.create_all``).

    The models import is deliberately lazy: it keeps module import order irrelevant and lets the
    API boot even when the models module or the database is temporarily unavailable. Any failure
    is logged and re-raised so the caller (``lifespan``) can decide to degrade instead of crash.
    """
    from app import models as models_module  # lazy on purpose: keeps import order irrelevant

    base: Any = getattr(models_module, "Base", None)
    if base is None:
        raise RuntimeError("app.models must define `class Base(DeclarativeBase)`")

    async with engine.begin() as conn:
        await conn.run_sync(base.metadata.create_all)
    logger.info("init_models: schema ensured (%d tables)", len(base.metadata.tables))


async def ping_db() -> bool:
    """``SELECT 1`` health probe. Returns ``False`` instead of raising."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health probes must never propagate
        logger.warning("ping_db failed: %s: %s", type(exc).__name__, exc)
        return False


async def dispose_engine() -> None:
    """Close the connection pool (safe to call when the engine never connected)."""
    try:
        await engine.dispose()
    except Exception as exc:  # noqa: BLE001 - shutdown must never raise
        logger.warning("engine.dispose failed: %s: %s", type(exc).__name__, exc)


__all__ = [
    "SessionLocal",
    "dispose_engine",
    "engine",
    "get_db",
    "get_session_factory",
    "init_models",
    "ping_db",
]

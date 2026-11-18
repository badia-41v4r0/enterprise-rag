"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine_options() -> dict[str, Any]:
    """Pool sizes per process, and the PgBouncer (transaction mode) settings when enabled (R18).

    Behind PgBouncer a server connection changes between transactions, so asyncpg's statement cache is off and
    every prepared statement gets a unique name. Nothing relies on session state: retrieval uses SET LOCAL only.
    """
    s = get_settings()
    opts: dict[str, Any] = {"pool_pre_ping": True, "pool_size": s.db_pool_size, "max_overflow": s.db_max_overflow}
    if s.db_pgbouncer:
        opts["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__erag_{uuid.uuid4().hex}__",
        }
    return opts


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, **engine_options())
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by the handlers that write."""
    async with get_sessionmaker()() as session:
        yield session

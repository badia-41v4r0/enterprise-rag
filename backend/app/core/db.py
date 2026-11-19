"""Async SQLAlchemy engine and session management.

Two pools per process (R20, found by the load test):
- the **request pool** (`get_sessionmaker`, `get_db`): one connection per request while it does database work;
- the **side pool** (`get_side_sessionmaker`, `get_side_engine`): short, self-committing operations that run
  *while a request already holds a connection*: rate-limit counters, session activity, budget reservations,
  generation leases. Taking those from the request pool made every request hold one connection and wait for a
  second under load, until the pool ran dry and requests failed. A separate pool cannot starve that way.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_side_engine: AsyncEngine | None = None
_side_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine_options(*, side: bool = False) -> dict[str, Any]:
    """Pool sizes per process, and the PgBouncer (transaction mode) settings when enabled (R18).

    Behind PgBouncer a server connection changes between transactions, so asyncpg's statement cache is off and
    every prepared statement gets a unique name. Nothing relies on session state: retrieval uses SET LOCAL only.
    """
    s = get_settings()
    size, overflow = (s.db_side_pool_size, s.db_side_max_overflow) if side else (s.db_pool_size, s.db_max_overflow)
    opts: dict[str, Any] = {"pool_pre_ping": True, "pool_size": size, "max_overflow": overflow}
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


def get_side_engine() -> AsyncEngine:
    global _side_engine, _side_sessionmaker
    if _side_engine is None:
        _side_engine = create_async_engine(get_settings().database_url, **engine_options(side=True))
        _side_sessionmaker = async_sessionmaker(_side_engine, expire_on_commit=False)
    return _side_engine


def get_side_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_side_engine()
    assert _side_sessionmaker is not None
    return _side_sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _side_engine, _side_sessionmaker
    if _engine is not None:
        await _engine.dispose()
    if _side_engine is not None:
        await _side_engine.dispose()
    _engine = _side_engine = None
    _sessionmaker = _side_sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by the handlers that write."""
    async with get_sessionmaker()() as session:
        yield session

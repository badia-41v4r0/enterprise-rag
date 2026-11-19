"""The application's database settings behind PgBouncer in transaction mode (R18, PLAN-v0.5 5.1).

Runs when ERAG_TEST_PGBOUNCER_URL points at a PgBouncer (pool_mode=transaction) in front of the test database:

    docker run -d --rm --name erag-test-pgbouncer --add-host=host.docker.internal:host-gateway \
        -p 127.0.0.1:16432:5432 -e DATABASE_URL=postgres://erag:erag@host.docker.internal:55432/erag_test \
        -e POOL_MODE=transaction -e AUTH_TYPE=scram-sha-256 -e DEFAULT_POOL_SIZE=5 edoburu/pgbouncer:v1.24.1-p1
    ERAG_TEST_PGBOUNCER_URL=postgresql+asyncpg://erag:erag@127.0.0.1:16432/erag_test uv run pytest ...

Checks the chosen setting (no asyncpg statement cache, unique prepared statement names): many concurrent
sessions running the same statements over a small server pool, and SET LOCAL staying inside its transaction.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.db import engine_options

URL = os.environ.get("ERAG_TEST_PGBOUNCER_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="ERAG_TEST_PGBOUNCER_URL not set (no PgBouncer to test against)")


async def test_app_engine_options_work_in_transaction_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_pgbouncer", True)
    engine = create_async_engine(URL, **engine_options())
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def worker(i: int) -> int:
        total = 0
        for j in range(15):
            async with sm() as s:
                # The same parameterized statement again and again: prepared statements must not collide
                # when PgBouncer hands this client another server connection.
                total += int(await s.scalar(text("SELECT CAST(:a AS int) + CAST(:b AS int)"), {"a": i, "b": j}))
                await s.execute(text("SET LOCAL hnsw.ef_search = 77"))
                assert await s.scalar(text("SHOW hnsw.ef_search")) == "77"
                await s.commit()
        return total

    try:
        results = await asyncio.gather(*(worker(i) for i in range(20)))
        assert results == [15 * i + sum(range(15)) for i in range(20)]
        async with sm() as s:  # SET LOCAL did not leak into another transaction
            assert await s.scalar(text("SHOW hnsw.ef_search")) != "77"
    finally:
        await engine.dispose()

"""Rate limits shared by every app replica (R17, PLAN-v0.5 3.2).

Callers use `RateLimiter(name, limit)` and `await limiter.allow(key)`. The counts live behind a `LimitStore`:
- `PostgresLimitStore` (the default): an UNLOGGED `rate_limit_counters` table, one row per key and fixed window.
  Each check is a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` on an autocommit connection, so it
  takes no lock inside the request's transaction. UNLOGGED means a database crash resets the counters, an
  accepted trade-off for rate limits (never for money: budgets use reservations, app.services.reservations).
- `MemoryLimitStore`: one process only, for unit tests.

The window is a sliding-window approximation: the current fixed window plus the previous one weighted by how
much of it still overlaps. A refused attempt also counts, so hammering an endpoint keeps it closed.
Another backend (for example Valkey) only has to implement `LimitStore`.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Protocol

from sqlalchemy import text

from app.core.db import get_engine


class LimitStore(Protocol):
    async def hit(self, key: str, window_start: int, previous_start: int) -> tuple[int, int]:
        """Count one attempt in `window_start`; return (hits in this window, hits in the previous window)."""
        ...

    async def reset(self, prefix: str) -> None: ...


_HIT = text(
    """
    WITH cur AS (
        INSERT INTO rate_limit_counters (key, window_start, hits) VALUES (:key, :w, 1)
        ON CONFLICT (key, window_start) DO UPDATE SET hits = rate_limit_counters.hits + 1
        RETURNING hits
    )
    SELECT (SELECT hits FROM cur),
           coalesce((SELECT hits FROM rate_limit_counters WHERE key = :key AND window_start = :prev), 0)
    """
)


class PostgresLimitStore:
    async def hit(self, key: str, window_start: int, previous_start: int) -> tuple[int, int]:
        async with get_engine().connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            row = (await conn.execute(_HIT, {"key": key, "w": window_start, "prev": previous_start})).one()
            return int(row[0]), int(row[1])

    async def reset(self, prefix: str) -> None:
        async with get_engine().connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(
                text("DELETE FROM rate_limit_counters WHERE key LIKE :p ESCAPE '\\'"),
                {"p": prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"},
            )


class MemoryLimitStore:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, int], int] = defaultdict(int)

    async def hit(self, key: str, window_start: int, previous_start: int) -> tuple[int, int]:
        self._hits[(key, window_start)] += 1
        return self._hits[(key, window_start)], self._hits.get((key, previous_start), 0)

    async def reset(self, prefix: str) -> None:
        for k in [k for k in self._hits if k[0].startswith(prefix)]:
            del self._hits[k]


_store: LimitStore = PostgresLimitStore()


def set_store(store: LimitStore) -> None:
    global _store
    _store = store


class RateLimiter:
    def __init__(self, name: str, limit: int, window_seconds: int = 60) -> None:
        self.name = name
        self.limit = limit
        self.window = window_seconds

    async def allow(self, key: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        start = int(now // self.window) * self.window
        current, previous = await _store.hit(f"{self.name}:{key}", start, start - self.window)
        overlap = 1.0 - (now - start) / self.window
        return previous * overlap + current <= self.limit

    async def reset(self) -> None:
        await _store.reset(f"{self.name}:")


async def reset_limits(prefix: str = "") -> None:
    """Forget the counts of every limiter whose name starts with `prefix` (tests and admin tooling)."""
    await _store.reset(prefix)

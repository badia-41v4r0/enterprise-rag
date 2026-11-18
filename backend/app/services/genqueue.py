"""Generation queue with fairness, across replicas (R17, PLAN-v0.5 3.2 and 3.3).

A model's endpoints form a pool (the provider's primary URL plus its `llm_endpoints`), each with a maximum of
concurrent generations. A request needs a lease on one endpoint before it streams:

- Leases live in `generation_leases` with an expiry; a heartbeat extends it while the request streams, so a
  replica that dies gives its slots back within `LEASE_TTL`.
- Requests that find the pool full wait in `generation_waiters`. When a slot frees, it goes to the waiter with
  the highest role priority, then the user with the fewest generations in flight, then the oldest waiter: one
  person running a script cannot starve the others.
- Decisions are made under a transaction-level advisory lock per pool, so replicas never over-allocate.
- A release sends `NOTIFY erag_genq, '<pool>'`; waiting replicas listen on one direct connection per process
  (not through PgBouncer) and also re-check every `POLL_SECONDS`, so a lost notification only costs latency.
- Within a pool, the endpoint with the fewest active leases is chosen.

A pool where no endpoint has a limit skips all of this: no database round-trip for the common local setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import GenerationLease, GenerationWaiter

log = logging.getLogger(__name__)

LEASE_TTL = timedelta(seconds=60)
HEARTBEAT_SECONDS = 20.0
WAITER_TTL = timedelta(seconds=30)
POLL_SECONDS = 1.0
CHANNEL = "erag_genq"


@dataclass(frozen=True)
class Endpoint:
    url: str
    max_concurrency: int | None  # None: unlimited


@dataclass
class Lease:
    id: uuid.UUID | None  # None when the pool is unlimited (no row)
    pool: str
    endpoint: str


class QueueTimeout(Exception):
    code = "queue_timeout"


# --- Notifications -------------------------------------------------------------------------------------------


class _Listener:
    """One LISTEN connection per process; wakes the waiters of a pool when a slot is released anywhere."""

    def __init__(self) -> None:
        self._conn: Any = None
        self._events: dict[str, set[asyncio.Event]] = {}
        self._lock = asyncio.Lock()
        self.failed = False

    async def subscribe(self, pool: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._events.setdefault(pool, set()).add(ev)
        await self._ensure()
        return ev

    def unsubscribe(self, pool: str, ev: asyncio.Event) -> None:
        self._events.get(pool, set()).discard(ev)

    def _wake(self, _conn: object, _pid: int, _channel: str, pool: str) -> None:
        for ev in list(self._events.get(pool, ())):
            ev.set()

    async def _ensure(self) -> None:
        if self._conn is not None or self.failed:
            return
        async with self._lock:
            if self._conn is not None or self.failed:
                return
            try:
                import asyncpg  # type: ignore[import-untyped]

                s = get_settings()
                dsn = (s.db_direct_url or s.database_url).replace("postgresql+asyncpg://", "postgresql://", 1)
                conn = await asyncpg.connect(dsn)
                await conn.add_listener(CHANNEL, self._wake)
                self._conn = conn
            except Exception:  # polling still works; say so once
                log.warning("generation queue: LISTEN unavailable, falling back to polling", exc_info=True)
                self.failed = True

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
        self._conn = None
        self.failed = False


listener = _Listener()


# --- Queue ---------------------------------------------------------------------------------------------------


def limited(endpoints: list[Endpoint]) -> bool:
    return any(e.max_concurrency is not None for e in endpoints)


def _lock(pool: str) -> Any:
    return text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(k="genq:" + pool)


async def _try_take(
    pool: str, endpoints: list[Endpoint], waiter_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> tuple[Lease | None, int]:
    """One attempt under the pool lock: (lease, 0) or (None, position in the queue, 1-based)."""
    async with get_sessionmaker()() as db:
        await db.execute(_lock(pool))
        await db.execute(delete(GenerationLease).where(GenerationLease.pool == pool, GenerationLease.expires_at <= now))
        await db.execute(
            delete(GenerationWaiter).where(GenerationWaiter.pool == pool, GenerationWaiter.seen_at <= now - WAITER_TTL)
        )
        await db.execute(update(GenerationWaiter).where(GenerationWaiter.id == waiter_id).values(seen_at=now))
        active = dict(
            (
                await db.execute(
                    select(GenerationLease.endpoint, func.count())
                    .where(GenerationLease.pool == pool)
                    .group_by(GenerationLease.endpoint)
                )
            ).all()
        )
        free = [e for e in endpoints if e.max_concurrency is None or active.get(e.url, 0) < e.max_concurrency]
        in_flight = (
            select(func.count())
            .select_from(GenerationLease)
            .where(GenerationLease.pool == pool, GenerationLease.user_id == GenerationWaiter.user_id)
            .scalar_subquery()
        )
        order = (
            await db.scalars(
                select(GenerationWaiter.id)
                .where(GenerationWaiter.pool == pool)
                .order_by(GenerationWaiter.priority.desc(), in_flight.asc(), GenerationWaiter.enqueued_at.asc())
            )
        ).all()
        position = order.index(waiter_id) + 1 if waiter_id in order else len(order) + 1
        slots = sum(
            (e.max_concurrency - active.get(e.url, 0)) if e.max_concurrency is not None else 10**6 for e in free
        )
        if free and position <= slots:
            endpoint = min(free, key=lambda e: active.get(e.url, 0))
            lease = GenerationLease(pool=pool, endpoint=endpoint.url, user_id=user_id, expires_at=now + LEASE_TTL)
            db.add(lease)
            await db.execute(delete(GenerationWaiter).where(GenerationWaiter.id == waiter_id))
            await db.commit()
            return Lease(lease.id, pool, endpoint.url), 0
        await db.commit()
        return None, max(position - slots, 1)


async def acquire(
    pool: str,
    endpoints: list[Endpoint],
    user_id: uuid.UUID,
    *,
    priority: int = 0,
    wait_seconds: float | None = None,
) -> AsyncIterator[int | Lease]:
    """Yield queue positions while waiting, then the lease.

    Raises QueueTimeout after `wait_seconds` (default: the setting)."""
    if not limited(endpoints):
        yield Lease(None, pool, endpoints[0].url)
        return
    timeout = get_settings().generation_queue_timeout_seconds if wait_seconds is None else wait_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiter = GenerationWaiter(pool=pool, user_id=user_id, priority=priority)
    now = datetime.now(UTC)
    waiter.enqueued_at = waiter.seen_at = now
    async with get_sessionmaker()() as db:
        db.add(waiter)
        await db.commit()
    event = await listener.subscribe(pool)
    last_position = 0
    try:
        while True:
            event.clear()
            lease, position = await _try_take(pool, endpoints, waiter.id, user_id, datetime.now(UTC))
            if lease is not None:
                yield lease
                return
            if position != last_position:
                last_position = position
                yield position
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise QueueTimeout(
                    f"All {pool} model slots are busy; your request waited {timeout:.0f}s. Please try again shortly."
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), min(POLL_SECONDS, remaining))
    finally:
        listener.unsubscribe(pool, event)
        async with get_sessionmaker()() as db:
            await db.execute(delete(GenerationWaiter).where(GenerationWaiter.id == waiter.id))
            await db.commit()


async def release(lease: Lease) -> None:
    if lease.id is None:
        return
    async with get_sessionmaker()() as db:
        await db.execute(delete(GenerationLease).where(GenerationLease.id == lease.id))
        await db.execute(text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": lease.pool})
        await db.commit()


async def _heartbeat(lease: Lease) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with get_sessionmaker()() as db:
            await db.execute(
                update(GenerationLease)
                .where(GenerationLease.id == lease.id)
                .values(expires_at=datetime.now(UTC) + LEASE_TTL)
            )
            await db.commit()


@contextlib.asynccontextmanager
async def held(lease: Lease) -> AsyncIterator[Lease]:
    """Keep a lease alive while the block runs, and release it afterwards whatever happens."""
    beat = asyncio.create_task(_heartbeat(lease)) if lease.id is not None else None
    try:
        yield lease
    finally:
        if beat is not None:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        await asyncio.shield(release(lease))


async def depth(pool: str | None = None) -> dict[str, int]:
    """Waiting requests per pool (a metric)."""
    async with get_sessionmaker()() as db:
        q = select(GenerationWaiter.pool, func.count()).group_by(GenerationWaiter.pool)
        if pool:
            q = q.where(GenerationWaiter.pool == pool)
        return {p: int(n) for p, n in (await db.execute(q)).all()}


async def purge_expired(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    async with get_sessionmaker()() as db:
        a = await db.execute(delete(GenerationLease).where(GenerationLease.expires_at <= now))
        b = await db.execute(delete(GenerationWaiter).where(GenerationWaiter.seen_at <= now - WAITER_TTL))
        await db.commit()
        return int(a.rowcount + b.rowcount)  # type: ignore[attr-defined]

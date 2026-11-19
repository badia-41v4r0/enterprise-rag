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
from app.core.db import get_side_sessionmaker
from app.models import GenerationLease, GenerationWaiter

log = logging.getLogger(__name__)

LEASE_TTL = timedelta(seconds=60)
HEARTBEAT_SECONDS = 20.0
WAITER_TTL = timedelta(seconds=30)
POLL_SECONDS = 1.0
FALLBACK_SECONDS = 3.0  # how often a waiter looks for slots freed without a release (expired leases)
CHANNEL = "erag_genq"


@dataclass(frozen=True)
class Endpoint:
    url: str
    max_concurrency: int | None  # None: unlimited


@dataclass
class Lease:
    id: uuid.UUID | None  # None when the pool is unlimited (no row); else the waiter's id (handoff)
    pool: str
    endpoint: str
    endpoints: tuple[Endpoint, ...] = ()  # the pool's capacities, so a release can hand the slot on


class QueueTimeout(Exception):
    code = "queue_timeout"


# --- Notifications -------------------------------------------------------------------------------------------


class _Listener:
    """One LISTEN connection per process. A grant notifies with the waiter's id, so only that request wakes."""

    def __init__(self) -> None:
        self._conn: Any = None
        self._events: dict[str, set[asyncio.Event]] = {}
        self._lock = asyncio.Lock()
        self.failed = False

    async def subscribe(self, key: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._events.setdefault(key, set()).add(ev)
        await self._ensure()
        return ev

    def unsubscribe(self, key: str, ev: asyncio.Event) -> None:
        events = self._events.get(key)
        if events is not None:
            events.discard(ev)
            if not events:
                del self._events[key]

    def _wake(self, _conn: object, _pid: int, _channel: str, key: str) -> None:
        for ev in list(self._events.get(key, ())):
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


def _key(pool: str) -> str:
    return "genq:" + pool


async def _dispatch(db: Any, pool: str, endpoints: list[Endpoint], now: datetime) -> int:
    """Hand free slots to waiters, in fair order (call with the pool lock held). Returns how many were granted.

    A grant is a lease whose id is the waiter's id: the waiter finds it by primary key, without the lock. This
    is the handoff that keeps the lock work per release constant however many requests wait (R20: with every
    waiter competing for the lock on each release, dispatch fell to a quarter of the pool's capacity)."""
    await db.execute(delete(GenerationLease).where(GenerationLease.pool == pool, GenerationLease.expires_at <= now))
    await db.execute(
        delete(GenerationWaiter).where(GenerationWaiter.pool == pool, GenerationWaiter.seen_at <= now - WAITER_TTL)
    )
    active: dict[str, int] = dict(
        (
            await db.execute(
                select(GenerationLease.endpoint, func.count())
                .where(GenerationLease.pool == pool)
                .group_by(GenerationLease.endpoint)
            )
        ).all()
    )
    in_flight = (
        select(func.count())
        .select_from(GenerationLease)
        .where(GenerationLease.pool == pool, GenerationLease.user_id == GenerationWaiter.user_id)
        .scalar_subquery()
    )
    granted = 0
    while True:
        free = [e for e in endpoints if e.max_concurrency is None or active.get(e.url, 0) < e.max_concurrency]
        if not free:
            break
        # Re-ranked after every grant: a user who just got a slot now has one more in flight.
        nxt = (
            await db.execute(
                select(GenerationWaiter.id, GenerationWaiter.user_id)
                .where(GenerationWaiter.pool == pool)
                .order_by(GenerationWaiter.priority.desc(), in_flight.asc(), GenerationWaiter.enqueued_at.asc())
                .limit(1)
            )
        ).first()
        if nxt is None:
            break
        endpoint = min(free, key=lambda e: active.get(e.url, 0))
        db.add(GenerationLease(id=nxt[0], pool=pool, endpoint=endpoint.url, user_id=nxt[1], expires_at=now + LEASE_TTL))
        await db.execute(delete(GenerationWaiter).where(GenerationWaiter.id == nxt[0]))
        await db.flush()
        await db.execute(text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": str(nxt[0])})
        active[endpoint.url] = active.get(endpoint.url, 0) + 1
        granted += 1
    return granted


async def _locked_dispatch(pool: str, endpoints: list[Endpoint], *, wait: bool) -> None:
    """Dispatch under the pool lock. With `wait=False` (periodic fallback) skip if someone else holds it."""
    async with get_side_sessionmaker()() as db:
        if wait:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _key(pool)})
        elif not await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": _key(pool)}):
            return
        await _dispatch(db, pool, endpoints, datetime.now(UTC))
        await db.commit()


async def _granted(waiter_id: uuid.UUID) -> str | None:
    async with get_side_sessionmaker()() as db:
        return await db.scalar(select(GenerationLease.endpoint).where(GenerationLease.id == waiter_id))


async def _position(waiter: GenerationWaiter) -> int:
    """Approximate place in the queue (priority, then age), and a heartbeat for the waiter row. No lock."""
    async with get_side_sessionmaker()() as db:
        await db.execute(
            update(GenerationWaiter).where(GenerationWaiter.id == waiter.id).values(seen_at=datetime.now(UTC))
        )
        ahead = await db.scalar(
            select(func.count())
            .select_from(GenerationWaiter)
            .where(
                GenerationWaiter.pool == waiter.pool,
                (GenerationWaiter.priority > waiter.priority)
                | (
                    (GenerationWaiter.priority == waiter.priority) & (GenerationWaiter.enqueued_at < waiter.enqueued_at)
                ),
            )
        )
        await db.commit()
        return int(ahead or 0) + 1


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
    waiter = GenerationWaiter(id=uuid.uuid4(), pool=pool, user_id=user_id, priority=priority)
    waiter.enqueued_at = waiter.seen_at = datetime.now(UTC)
    async with get_side_sessionmaker()() as db:
        db.add(waiter)
        await db.commit()
    event = await listener.subscribe(str(waiter.id))
    granted = False
    last_position = 0
    last_fallback = loop.time()
    try:
        await _locked_dispatch(pool, endpoints, wait=True)  # a free slot goes to the fairest waiter, maybe us
        while True:
            event.clear()
            endpoint = await _granted(waiter.id)
            if endpoint is not None:
                granted = True
                yield Lease(waiter.id, pool, endpoint, tuple(endpoints))
                return
            if loop.time() - last_fallback >= FALLBACK_SECONDS:
                # Slots freed without a release (a dead replica's lease expired) are only found by looking.
                last_fallback = loop.time()
                await _locked_dispatch(pool, endpoints, wait=False)
                if await _granted(waiter.id) is not None:
                    continue
            position = await _position(waiter)
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
        listener.unsubscribe(str(waiter.id), event)
        async with get_side_sessionmaker()() as db:
            await db.execute(delete(GenerationWaiter).where(GenerationWaiter.id == waiter.id))
            # Granted at the last moment but never used (timeout, cancelled stream): give the slot back.
            abandoned = 0
            if not granted:
                res = await db.execute(delete(GenerationLease).where(GenerationLease.id == waiter.id))
                abandoned = int(res.rowcount)  # type: ignore[attr-defined]
            await db.commit()
        if abandoned:
            await asyncio.shield(_locked_dispatch(pool, endpoints, wait=True))


async def release(lease: Lease) -> None:
    """Free the slot and hand it straight to the next waiter (same transaction, under the pool lock)."""
    if lease.id is None:
        return
    async with get_side_sessionmaker()() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _key(lease.pool)})
        await db.execute(delete(GenerationLease).where(GenerationLease.id == lease.id))
        if lease.endpoints:
            await _dispatch(db, lease.pool, list(lease.endpoints), datetime.now(UTC))
        await db.commit()


async def _heartbeat(lease: Lease) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with get_side_sessionmaker()() as db:
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
    async with get_side_sessionmaker()() as db:
        q = select(GenerationWaiter.pool, func.count()).group_by(GenerationWaiter.pool)
        if pool:
            q = q.where(GenerationWaiter.pool == pool)
        return {p: int(n) for p, n in (await db.execute(q)).all()}


async def purge_expired(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    async with get_side_sessionmaker()() as db:
        a = await db.execute(delete(GenerationLease).where(GenerationLease.expires_at <= now))
        b = await db.execute(delete(GenerationWaiter).where(GenerationWaiter.seen_at <= now - WAITER_TTL))
        await db.commit()
        return int(a.rowcount + b.rowcount)  # type: ignore[attr-defined]

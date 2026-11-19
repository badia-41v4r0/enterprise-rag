"""The generation queue under contention (R20): the limit holds, freed slots are handed straight to the next
waiter (no herd of waiters competing for the lock), and a slot granted to a request that gave up comes back."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationLease, GenerationWaiter
from app.services import genqueue
from app.services.genqueue import Endpoint, Lease

pytestmark = pytest.mark.db


async def test_many_waiters_never_exceed_the_limit_and_all_finish(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 4)]
    in_flight = 0
    peak = 0

    async def request(user: uuid.UUID) -> None:
        nonlocal in_flight, peak
        lease: Lease | None = None
        async for slot in genqueue.acquire("burst", eps, user, wait_seconds=60):
            if isinstance(slot, Lease):
                lease = slot
        assert lease is not None
        async with genqueue.held(lease):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.1)  # the "generation"
            in_flight -= 1

    users = [uuid.uuid4() for _ in range(10)]
    start = time.perf_counter()
    await asyncio.gather(*(request(users[i % 10]) for i in range(40)))
    elapsed = time.perf_counter() - start
    assert peak == 4
    # 40 generations of 0.1 s on 4 slots need 1 s of pure generation; handoff keeps dispatch overhead small.
    assert elapsed < 8, f"dispatch too slow: {elapsed:.1f}s"
    assert await db.scalar(select(func.count()).select_from(GenerationLease)) == 0
    assert await db.scalar(select(func.count()).select_from(GenerationWaiter)) == 0


async def test_a_slot_granted_to_a_request_that_gave_up_goes_to_the_next(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 1)]

    async def take(user: uuid.UUID, wait: float) -> Lease:
        async for slot in genqueue.acquire("give-up", eps, user, wait_seconds=wait):
            if isinstance(slot, Lease):
                return slot
        raise AssertionError("no lease")

    holder = await take(uuid.uuid4(), 5)
    quitter = asyncio.create_task(take(uuid.uuid4(), 30))
    patient = asyncio.create_task(take(uuid.uuid4(), 30))
    for _ in range(100):
        if await db.scalar(select(func.count()).select_from(GenerationWaiter)) == 2:
            break
        await db.commit()
        await asyncio.sleep(0.05)
    await genqueue.release(holder)  # handed to the quitter (first in line) ...
    quitter.cancel()  # ... which is cancelled before it uses it
    with pytest.raises(asyncio.CancelledError):
        await quitter
    lease = await asyncio.wait_for(patient, 10)  # the slot came back and went to the patient one
    assert lease.endpoint == "http://gpu"
    await genqueue.release(lease)

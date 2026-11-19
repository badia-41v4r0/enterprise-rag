"""Shared state for several app replicas (R17): rate limits, budget reservations, the fair generation queue and
endpoint pools. "Replicas" here are independent sessions and limiter instances on the same database."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ratelimit import MemoryLimitStore, RateLimiter, reset_limits, set_store
from app.llm.providers.base import ProviderError, TextDelta
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.models import BudgetReservation, Classification, GenerationLease, GenerationWaiter, LlmUsage
from app.services import genqueue, reservations
from app.services.genqueue import Endpoint, Lease
from app.services.spend import record_spend
from app.services.usage import LimitReached, ModelLimits, reserve

pytestmark = pytest.mark.db


# --- Rate limits --------------------------------------------------------------------------------------------


async def test_rate_limit_is_shared_by_every_replica(db: AsyncSession) -> None:
    await reset_limits()
    replica_a, replica_b = RateLimiter("login", 3), RateLimiter("login", 3)
    now = 1_000_020.0
    results = [await r.allow("1.2.3.4:x@y", now=now) for r in (replica_a, replica_b, replica_a, replica_b)]
    assert results == [True, True, True, False]
    assert await RateLimiter("login", 3).allow("other-key", now=now)  # keys are independent


async def test_sliding_window_counts_the_overlap_of_the_previous_window() -> None:
    set_store(MemoryLimitStore())
    try:
        r = RateLimiter("t", 4, window_seconds=60)
        assert all([await r.allow("k", now=59.0) for _ in range(4)])
        # 15 s into the next window, 75% of the previous 4 hits still count: 3 + 1 = 4 allowed, then refused.
        assert await r.allow("k", now=75.0)
        assert not await r.allow("k", now=75.0)
        # Two windows later the past no longer counts.
        assert await r.allow("k", now=181.0)
    finally:
        from app.core.ratelimit import PostgresLimitStore

        set_store(PostgresLimitStore())


# --- Budget reservations ------------------------------------------------------------------------------------


def priced(**kw: object) -> ModelLimits:
    base: dict[str, object] = {
        "provider": "acme",
        "model": "m",
        "metered": True,
        "max_output_tokens": 1000,
        "input_usd_per_mtok": 1000.0,
        "output_usd_per_mtok": 1000.0,
        "price_known": True,
    }
    base.update(kw)
    return ModelLimits(**base)  # type: ignore[arg-type]


async def test_concurrent_requests_cannot_overspend_a_budget(db: AsyncSession) -> None:
    # Worst case of each request: (1000 in + 1000 out) x $1000/Mtok = $2.00. Budget $5: two fit, not three.
    lim = priced(provider_monthly_usd=5.0)

    async def one() -> bool:
        try:
            await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
            return True
        except LimitReached:
            return False

    outcomes = await asyncio.gather(*(one() for _ in range(6)))
    assert sum(outcomes) == 2
    assert await db.scalar(select(func.count(func.distinct(BudgetReservation.request_id)))) == 2


async def test_settling_releases_the_worst_case_and_counts_the_real_cost(db: AsyncSession) -> None:
    lim = priced(provider_monthly_usd=5.0)
    first = await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    second = await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    with pytest.raises(LimitReached):
        await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    # The first request really cost $0.10: its $2.00 hold goes, $0.10 is spent.
    assert first.request_id is not None and second.request_id is not None
    await record_spend(db, "acme", 0.10)
    await reservations.release(db, first.request_id)
    await db.commit()
    assert (await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)).request_id is not None


async def test_a_dead_request_stops_holding_its_reservation_when_it_expires(db: AsyncSession) -> None:
    lim = priced(provider_monthly_usd=3.0)
    held = await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    with pytest.raises(LimitReached):
        await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    await db.execute(
        update(BudgetReservation)
        .where(BudgetReservation.request_id == held.request_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    await reserve(lim, user_id=None, role_ids=set(), input_tokens=1000)
    assert await reservations.purge_expired(db) == 2  # provider and model rows of the dead request
    await db.commit()


# --- Fair generation queue ----------------------------------------------------------------------------------


async def take(pool: str, eps: list[Endpoint], user: uuid.UUID, priority: int = 0, wait: float = 5.0) -> Lease:
    lease: Lease | None = None
    async for slot in genqueue.acquire(pool, eps, user, priority=priority, wait_seconds=wait):
        if isinstance(slot, Lease):
            lease = slot
    assert lease is not None
    return lease


async def started_waiting(db: AsyncSession, pool: str, n: int) -> None:
    for _ in range(100):
        count = await db.scalar(select(func.count()).select_from(GenerationWaiter).where(GenerationWaiter.pool == pool))
        await db.commit()
        if count == n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {n} waiters")


async def test_unlimited_pool_needs_no_database(db: AsyncSession) -> None:
    lease = await take("free", [Endpoint("http://a", None)], uuid.uuid4())
    assert lease.id is None and lease.endpoint == "http://a"
    assert await db.scalar(select(func.count()).select_from(GenerationLease)) == 0


async def test_slot_goes_to_the_user_with_fewest_in_flight_not_the_oldest_waiter(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 2)]
    heavy, light = uuid.uuid4(), uuid.uuid4()
    first, _second = await take("p", eps, heavy), await take("p", eps, heavy)
    heavy_third = asyncio.create_task(take("p", eps, heavy))
    await started_waiting(db, "p", 1)
    light_first = asyncio.create_task(take("p", eps, light))
    await started_waiting(db, "p", 2)
    await genqueue.release(first)
    done, _ = await asyncio.wait({heavy_third, light_first}, return_when=asyncio.FIRST_COMPLETED)
    assert done == {light_first}  # heavy still has one generation in flight; light has none
    await genqueue.release(light_first.result())
    await asyncio.wait_for(heavy_third, 5)
    await genqueue.release(heavy_third.result())


async def test_role_priority_goes_first(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 1)]
    holder = await take("q", eps, uuid.uuid4())
    normal = asyncio.create_task(take("q", eps, uuid.uuid4()))
    await started_waiting(db, "q", 1)
    support = asyncio.create_task(take("q", eps, uuid.uuid4(), priority=10))
    await started_waiting(db, "q", 2)
    await genqueue.release(holder)
    done, _ = await asyncio.wait({normal, support}, return_when=asyncio.FIRST_COMPLETED)
    assert done == {support}
    await genqueue.release(support.result())
    await genqueue.release(await asyncio.wait_for(normal, 5))


async def test_positions_are_reported_and_waiting_times_out(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 1)]
    holder = await take("r", eps, uuid.uuid4())
    positions: list[int] = []
    with pytest.raises(genqueue.QueueTimeout):
        async for slot in genqueue.acquire("r", eps, uuid.uuid4(), wait_seconds=0.5):
            assert isinstance(slot, int)
            positions.append(slot)
    assert positions == [1]
    assert await db.scalar(select(func.count()).select_from(GenerationWaiter)) == 0  # cleaned up
    await genqueue.release(holder)


async def test_a_dead_replicas_lease_expires_and_the_slot_comes_back(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu", 1)]
    await take("s", eps, uuid.uuid4())  # never released: its replica "died"
    await db.execute(update(GenerationLease).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    lease = await take("s", eps, uuid.uuid4(), wait=2.0)
    await genqueue.release(lease)


async def test_the_least_loaded_endpoint_is_chosen(db: AsyncSession) -> None:
    eps = [Endpoint("http://gpu-a", 2), Endpoint("http://gpu-b", 2)]
    a, b = await take("t", eps, uuid.uuid4()), await take("t", eps, uuid.uuid4())
    assert {a.endpoint, b.endpoint} == {"http://gpu-a", "http://gpu-b"}
    for lease in (a, b):
        await genqueue.release(lease)


# --- Endpoint pool in the provider ---------------------------------------------------------------------------


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


async def test_provider_only_sends_to_its_own_endpoints() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "hi"}}]}))

    p = OpenAICompatProvider(
        provider="ollama",
        base_url="http://10.0.0.1:11434/v1",
        model="m",
        locality="local",
        max_classification=Classification.confidential,
        transport=httpx.MockTransport(handler),
        endpoints=[("http://10.0.0.1:11434/v1", 2), ("http://10.0.0.2:11434/v1", 2)],
    )
    out = [
        e async for e in p.stream_chat([], classification=Classification.public, endpoint="http://10.0.0.2:11434/v1")
    ]
    assert seen == ["http://10.0.0.2:11434/v1/chat/completions"] and isinstance(out[0], TextDelta)
    with pytest.raises(ProviderError, match="not one of its endpoints"):
        _ = [e async for e in p.stream_chat([], classification=Classification.public, endpoint="http://evil:1/v1")]


async def test_usage_is_recorded_when_the_model_fails_mid_stream(db: AsyncSession) -> None:
    """A stream that breaks after the prompt was sent still costs tokens: they are recorded (estimated) and the
    reservation is settled, not left to expire."""
    from app.llm.router import ModelRouter
    from app.orchestrator.chat import ChatOrchestrator
    from tests.factories import make_user

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"content":"par"}}]}\n\ndata: {"error":"boom"}\n\n'
        )

    p = OpenAICompatProvider(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="m",
        locality="local",
        max_classification=Classification.confidential,
        transport=httpx.MockTransport(handler),
    )
    u = await make_user(db, "u@x.test")
    await db.commit()

    from app.authz.policy import EffectivePermissions
    from app.models import Conversation
    from app.orchestrator.chat import PreparedChat
    from app.rag.context_builder import BuiltContext
    from app.rag.retrieval import RetrievalResult

    orch = ChatOrchestrator(db, embedder=None, router=ModelRouter([p]))  # type: ignore[arg-type]

    # Skip retrieval: a prepared chat with a provider chosen, as when there is authorized context.
    async def fake_prepare(*_a: object, **_k: object) -> PreparedChat:
        conv = Conversation(user_id=u.id, title="t")
        db.add(conv)
        await db.flush()
        return PreparedChat(
            conversation=conv,
            question="hello there",
            retrieval=RetrievalResult([]),
            context=BuiltContext(),
            history=[],
            history_classification=Classification.public,
            withheld_message_ids=[],
            tools=[],
            classification=Classification.public,
            provider=p,
            refusal=None,
            messages=[{"role": "user", "content": "hello there"}],
        )

    orch.prepare = fake_prepare  # type: ignore[method-assign]
    events = [e async for e in orch.run(u, EffectivePermissions.none(u.id), "hello there")]
    assert any(e["type"] == "error" for e in events)
    row = await db.scalar(select(LlmUsage).where(LlmUsage.user_id == u.id))
    assert row is not None and row.requests == 1 and row.prompt_tokens > 0
    assert await db.scalar(select(func.count()).select_from(BudgetReservation)) == 0

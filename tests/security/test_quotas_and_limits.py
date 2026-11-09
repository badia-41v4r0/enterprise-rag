"""Cost limits, token accounting and quotas (PLAN-v0.3 R6, ADR 0026, ADR 0027).

Real Postgres and real counters; no model is involved here (the real-Ollama path is in
tests/providers/test_provider_registry_real.py). Asserts the preventive worst-case check, the quota
semantics (per-user override, highest standard cap among roles, every collective pool charged, several
roles at once, reset at day and month change), privacy of usage, step-up and the change history.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, LlmUsage, Quota, Session
from app.services import usage
from app.services.spend import record_spend
from app.services.usage import (
    LimitReached,
    Measured,
    ModelLimits,
    QuotaExceeded,
    charge_pools,
    measure,
    preflight,
    preventive_input_tokens,
    record_usage,
)
from tests.factories import login, make_role, make_user

pytestmark = pytest.mark.db

D1 = datetime(2026, 3, 10, 12, tzinfo=UTC)
D2 = datetime(2026, 3, 11, 0, 0, 1, tzinfo=UTC)  # next UTC day, same month
M2 = datetime(2026, 4, 1, 0, 0, 1, tzinfo=UTC)  # next month


def lim(**kw: object) -> ModelLimits:
    base: dict[str, object] = {"provider": "acme", "model": "m1", "metered": True, "max_output_tokens": 100}
    base.update(kw)
    return ModelLimits(**base)  # type: ignore[arg-type]


async def use(
    db: AsyncSession,
    user: uuid.UUID | None,
    tokens: int,
    cost: float = 0.0,
    *,
    now: datetime = D1,
    provider: str = "acme",
    model: str = "m1",
) -> None:
    await record_usage(
        db, provider=provider, model=model, user_id=user, measured=Measured(tokens, 0, cost, False), now=now
    )
    await db.commit()


# --- Measuring --------------------------------------------------------------------------------------------


def test_measure_prefers_reported_usage_and_flags_estimates() -> None:
    priced = lim(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)
    msgs = [{"role": "user", "content": "x" * 400}]
    m = measure({"prompt_tokens": 1000, "completion_tokens": 500}, priced, msgs, None, 10)
    assert (m.prompt_tokens, m.completion_tokens, m.estimated) == (1000, 500, False)
    assert m.cost_usd == pytest.approx((1000 * 1 + 500 * 2) / 1e6)  # tokens x price
    m = measure({"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.5}, priced, msgs, None, 10)
    assert m.cost_usd == 0.5  # the provider's reported cost wins
    m = measure({}, priced, msgs, None, 40)  # nothing reported: characters / 4, flagged
    assert m.estimated and m.completion_tokens == 10 and m.prompt_tokens > 100
    assert preventive_input_tokens(msgs) > m.prompt_tokens  # the check before sending is more conservative


# --- Provider and model limits (worst case, before sending) --------------------------------------------


async def test_worst_case_cost_is_refused_before_sending(db: AsyncSession) -> None:
    # 1000 input tokens at $10/M + 100 output tokens at $30/M = $0.013 worst case.
    priced = lim(input_usd_per_mtok=10.0, output_usd_per_mtok=30.0, provider_monthly_usd=0.02)
    await preflight(db, priced, user_id=None, role_ids=set(), input_tokens=1000, now=D1)
    await record_spend(db, "acme", 0.01, D1)
    await db.commit()
    with pytest.raises(LimitReached, match="Monthly spend limit for acme"):
        await preflight(db, priced, user_id=None, role_ids=set(), input_tokens=1000, now=D1)
    # A cheaper request still fits in what remains; next month everything is fresh.
    await preflight(db, priced, user_id=None, role_ids=set(), input_tokens=10, now=D1)
    await preflight(db, priced, user_id=None, role_ids=set(), input_tokens=1000, now=M2)
    with pytest.raises(LimitReached):  # a ceiling of 0 blocks everything, as before
        await preflight(db, lim(provider_monthly_usd=0.0), user_id=None, role_ids=set(), input_tokens=1, now=D1)


async def test_size_turn_and_token_limits(db: AsyncSession) -> None:
    with pytest.raises(LimitReached, match="too large"):
        await preflight(db, lim(max_input_tokens=50), user_id=None, role_ids=set(), input_tokens=51)
    with pytest.raises(LimitReached, match="per-turn"):
        await preflight(db, lim(max_turn_tokens=500), user_id=None, role_ids=set(), input_tokens=100, turn_tokens=350)
    await preflight(db, lim(max_turn_tokens=500), user_id=None, role_ids=set(), input_tokens=100, turn_tokens=300)
    await use(db, None, 900)
    capped = lim(provider_tokens_per_day=1000)
    with pytest.raises(LimitReached, match="token limit of acme for today"):
        await preflight(db, capped, user_id=None, role_ids=set(), input_tokens=1, now=D1)
    await preflight(db, capped, user_id=None, role_ids=set(), input_tokens=1, now=D2)  # new day
    monthly = lim(provider_tokens_per_month=1500)
    await use(db, None, 500, now=D2)
    with pytest.raises(LimitReached, match="this month"):
        await preflight(db, monthly, user_id=None, role_ids=set(), input_tokens=1, now=D2)
    # A per-model cap counts only that model.
    model_cap = lim(model="m2", model_tokens_per_day=50, max_output_tokens=10)
    await preflight(db, model_cap, user_id=None, role_ids=set(), input_tokens=1, now=D1)
    await use(db, None, 60, model="m2")
    with pytest.raises(LimitReached, match="acme:m2"):
        await preflight(db, model_cap, user_id=None, role_ids=set(), input_tokens=1, now=D1)


# --- Quotas ---------------------------------------------------------------------------------------------


async def quota(db: AsyncSession, **kw: object) -> Quota:
    q = Quota(**kw)
    db.add(q)
    await db.commit()
    return q


async def test_per_user_override_wins_over_role_standard_caps(db: AsyncSession) -> None:
    eng, sales = await make_role(db, "Engineering"), await make_role(db, "Sales")
    ana = await make_user(db, "ana@acme.test", roles=[eng, sales])
    bob = await make_user(db, "bob@acme.test", roles=[eng])
    await quota(db, subject="role", role_id=eng.id, kind="standard", tokens_per_day=1000)
    await quota(db, subject="role", role_id=sales.id, kind="standard", tokens_per_day=3000)
    roles = {eng.id, sales.id}
    caps = await usage.applicable_caps(db, ana.id, roles, "acme")
    assert len(caps) == 1 and caps[0].limits["tokens_per_day"] == 3000  # the highest standard cap
    # Standard caps are counted per person: Bob's usage does not touch Ana's allowance.
    await use(db, bob.id, 950)
    await preflight(db, lim(), user_id=ana.id, role_ids=roles, input_tokens=100, now=D1)
    with pytest.raises(QuotaExceeded, match="Your daily allowance is used up; it resets at 00:00 UTC"):
        await preflight(db, lim(), user_id=bob.id, role_ids={eng.id}, input_tokens=100, now=D1)
    # A personal override replaces the role caps entirely (lower here).
    await quota(db, subject="user", user_id=ana.id, kind="standard", tokens_per_day=150)
    caps = await usage.applicable_caps(db, ana.id, roles, "acme")
    assert [c.source for c in caps] == ["user override"]
    with pytest.raises(QuotaExceeded):
        await preflight(db, lim(), user_id=ana.id, role_ids=roles, input_tokens=100, now=D1)  # 100 + 100 > 150
    # Reset at the UTC day boundary.
    await use(db, bob.id, 0, now=D2)
    await preflight(db, lim(), user_id=bob.id, role_ids={eng.id}, input_tokens=100, now=D2)


async def test_a_role_without_a_value_in_a_dimension_means_no_limit_there(db: AsyncSession) -> None:
    a, b = await make_role(db, "A"), await make_role(db, "B")
    u = await make_user(db, "u@acme.test", roles=[a, b])
    await quota(db, subject="role", role_id=a.id, kind="standard", tokens_per_day=100, usd_per_month=1.0)
    await quota(db, subject="role", role_id=b.id, kind="standard", usd_per_month=2.0)
    caps = await usage.applicable_caps(db, u.id, {a.id, b.id}, "acme")
    assert caps[0].limits == {
        "tokens_per_day": None,
        "tokens_per_month": None,
        "usd_per_day": None,
        "usd_per_month": 2.0,
    }


async def test_collective_pool_is_shared_and_every_pool_is_charged(db: AsyncSession) -> None:
    eng, emp = await make_role(db, "Engineering"), await make_role(db, "Employee")
    ana = await make_user(db, "ana@acme.test", roles=[eng, emp])
    bob = await make_user(db, "bob@acme.test", roles=[eng, emp])
    cid = await make_user(db, "cid@acme.test", roles=[emp])
    pool = await quota(db, subject="role", role_id=eng.id, kind="collective", tokens_per_day=1000)
    company = await quota(db, subject="role", role_id=emp.id, kind="collective", tokens_per_month=5000)
    caps = await usage.applicable_caps(db, ana.id, {eng.id, emp.id}, "acme")
    assert {c.quota_id for c in caps} == {pool.id, company.id}
    await charge_pools(db, caps, 900, 0.0, D1)  # Ana's request: both pools charged
    await db.commit()
    assert await usage.pool_used(db, pool.id, "2026-03-10") == (900, 0.0)
    assert await usage.pool_used(db, company.id, "2026-03") == (900, 0.0)
    # Bob shares the Engineering pool: his worst case (100 + 100) no longer fits.
    with pytest.raises(QuotaExceeded, match="shared daily allowance of the Engineering role"):
        await preflight(db, lim(), user_id=bob.id, role_ids={eng.id, emp.id}, input_tokens=100, now=D1)
    # Cid is not an engineer: only the company pool applies to him.
    await preflight(db, lim(), user_id=cid.id, role_ids={emp.id}, input_tokens=100, now=D1)
    # Next day the daily pool is fresh, the monthly company pool is not.
    await preflight(db, lim(), user_id=bob.id, role_ids={eng.id, emp.id}, input_tokens=100, now=D2)
    caps_cid = await usage.applicable_caps(db, cid.id, {emp.id}, "acme")
    await charge_pools(db, caps_cid, 4000, 0.0, D2)
    await db.commit()
    with pytest.raises(QuotaExceeded, match="shared monthly allowance of the Employee role"):
        await preflight(db, lim(), user_id=cid.id, role_ids={emp.id}, input_tokens=10, now=D2)
    await preflight(db, lim(), user_id=cid.id, role_ids={emp.id}, input_tokens=10, now=M2)  # new month


async def test_provider_scoped_quota_applies_only_to_that_provider(db: AsyncSession) -> None:
    r = await make_role(db, "R")
    u = await make_user(db, "u@acme.test", roles=[r])
    await quota(db, subject="role", role_id=r.id, kind="standard", provider="acme", usd_per_month=0.001)
    priced = lim(input_usd_per_mtok=1.0, output_usd_per_mtok=1.0)
    await use(db, u.id, 100, 0.001)
    with pytest.raises(QuotaExceeded, match="monthly allowance for acme"):
        await preflight(db, priced, user_id=u.id, role_ids={r.id}, input_tokens=10, now=D1)
    await preflight(db, lim(provider="other"), user_id=u.id, role_ids={r.id}, input_tokens=10, now=D1)


# --- API: step-up, change history, privacy of usage ------------------------------------------------------


async def _admin(db: AsyncSession, client: httpx.AsyncClient) -> uuid.UUID:
    u = await make_user(db, "admin@acme.test", admin=True)
    await db.commit()
    csrf = await login(client, "admin@acme.test")
    client.headers["X-CSRF-Token"] = csrf
    return u.id


async def _age(db: AsyncSession, user_id: uuid.UUID) -> None:
    stale = datetime.now(UTC) - timedelta(minutes=10)
    await db.execute(update(Session).where(Session.user_id == user_id).values(reauth_at=stale))
    await db.commit()


async def test_quota_api_needs_step_up_and_is_audited(db: AsyncSession, client: httpx.AsyncClient) -> None:
    eng = await make_role(db, "Engineering")
    await db.commit()
    admin_id = await _admin(db, client)
    body = {"subject": "role", "role_id": str(eng.id), "kind": "collective", "tokens_per_day": 1000}
    r = await client.post("/api/admin/quotas", json=body)
    assert r.status_code == 201, r.text
    qid = r.json()["id"]
    assert r.json()["used"]["tokens_per_day"] == 0
    assert (await client.post("/api/admin/quotas", json=body)).status_code == 409
    bad = {"subject": "user", "user_id": str(admin_id), "kind": "collective", "tokens_per_day": 1}
    assert (await client.post("/api/admin/quotas", json=bad)).status_code == 422
    assert (await client.post("/api/admin/quotas", json={**body, "provider": "nope"})).status_code == 422
    await _age(db, admin_id)
    assert (await client.patch(f"/api/admin/quotas/{qid}", json={"tokens_per_day": 9})).json()["detail"] == (
        "step_up_required"
    )
    assert (await client.delete(f"/api/admin/quotas/{qid}")).status_code == 403
    assert (await client.post("/api/auth/step-up", json={"password": "correct-horse-battery"})).status_code == 200
    r = await client.patch(f"/api/admin/quotas/{qid}", json={"tokens_per_day": 5000})
    assert r.status_code == 200 and r.json()["tokens_per_day"] == 5000
    history = (await client.get("/api/admin/limits/history")).json()
    assert [h["action"] for h in history][:2] == ["admin.quota.update", "admin.quota.create"]
    assert history[0]["details"]["changes"]["tokens_per_day"] == {"from": 1000, "to": 5000}
    assert (await client.delete(f"/api/admin/quotas/{qid}")).status_code == 204
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "admin.quota.delete")) is not None


async def test_admin_may_raise_provider_limits_with_step_up(db: AsyncSession, client: httpx.AsyncClient) -> None:
    from app.core.config import get_settings

    admin_id = await _admin(db, client)
    listing = (await client.get("/api/admin/providers")).json()
    orow = next(p for p in listing["providers"] if p["slug"] == "openrouter")
    cap = get_settings().openrouter_monthly_budget_usd
    assert orow["monthly_budget_usd"] == cap and orow["daily_request_limit"] == 100
    r = await client.patch(f"/api/admin/providers/{orow['id']}", json={"monthly_budget_usd": cap * 10})
    assert r.status_code == 200 and r.json()["monthly_budget_usd"] == pytest.approx(cap * 10)  # raised (D2)
    # The legacy endpoint and the registry agree.
    assert (await client.get("/api/admin/providers/openrouter")).json()["monthly_budget_usd"] == pytest.approx(cap * 10)
    await _age(db, admin_id)
    r = await client.patch(f"/api/admin/providers/{orow['id']}", json={"tokens_per_day": 10})
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    model = orow["models"][0]
    r = await client.patch(f"/api/admin/providers/{orow['id']}/models/{model['id']}", json={"input_usd_per_mtok": 1})
    assert r.status_code == 403
    assert (await client.post("/api/auth/step-up", json={"password": "correct-horse-battery"})).status_code == 200
    r = await client.patch(
        f"/api/admin/providers/{orow['id']}/models/{model['id']}",
        json={"input_usd_per_mtok": 0.5, "output_usd_per_mtok": 1.5, "max_output_tokens": 256},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["input_usd_per_mtok"], r.json()["price_source"], r.json()["max_output_tokens"]) == (
        0.5,
        "admin",
        256,
    )
    actions = [h["action"] for h in (await client.get("/api/admin/limits/history")).json()]
    assert {"admin.limits.provider", "admin.limits.model", "admin.price.update"} <= set(actions)


async def test_users_see_only_their_own_usage(db: AsyncSession, client: httpx.AsyncClient) -> None:
    eng = await make_role(db, "Engineering")
    ana = await make_user(db, "ana@acme.test", roles=[eng])
    bob = await make_user(db, "bob@acme.test", roles=[eng])
    pool = await quota(db, subject="role", role_id=eng.id, kind="collective", tokens_per_day=1000)
    await quota(db, subject="role", role_id=eng.id, kind="standard", tokens_per_month=10_000)
    now = datetime.now(UTC)
    await use(db, bob.id, 700, now=now)
    await charge_pools(db, await usage.applicable_caps(db, bob.id, {eng.id}, "acme"), 700, 0.0, now)
    await use(db, ana.id, 42, now=now)
    await db.commit()
    await login(client, "ana@acme.test")
    me = (await client.get("/api/usage/me")).json()
    assert me["today"]["tokens"] == 42 and me["this_month"]["tokens"] == 42
    by_kind = {a["kind"]: a for a in me["allowances"]}
    assert by_kind["standard"]["used"]["tokens_per_month"] == 42
    # The shared pool: limits and whether it has room, never how much colleagues used.
    assert "used" not in by_kind["collective"] and by_kind["collective"]["exhausted"] == []
    assert "700" not in str(me)
    assert (await client.get("/api/admin/usage")).status_code == 403
    assert (await client.get(f"/api/admin/users/{bob.id}/usage")).status_code == 403
    # The admin sees aggregates and per-user figures (never text: the usage tables hold none).
    admin = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    await _admin(db, admin)
    agg = (await admin.get("/api/admin/usage")).json()
    assert {u["email"]: u["today"]["tokens"] for u in agg["users"]} == {"bob@acme.test": 700, "ana@acme.test": 42}
    assert agg["pools"][0]["id"] == str(pool.id) and agg["pools"][0]["used"]["tokens_per_day"] == 700
    assert agg["pools"][0]["members"] == 2
    columns = set(LlmUsage.__table__.columns.keys())
    assert not columns & {"prompt", "question", "answer", "content", "text"}
    await admin.aclose()

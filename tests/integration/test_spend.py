"""Spend ceiling and free-only guard (ADR 0016). Real Postgres; no model is involved."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import registry
from app.llm.providers.base import ProviderError
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.router import build_providers, validate_openrouter_model
from app.models import Classification, LlmProvider
from app.services.spend import (
    BudgetExceeded,
    cost_from_usage,
    ensure_budget,
    month_spend,
    record_spend,
    requests_today,
    reserve_request,
)

pytestmark = pytest.mark.db

JAN = datetime(2026, 1, 15, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)


def test_cost_from_usage() -> None:
    assert cost_from_usage({"cost": 0.0042}) == 0.0042
    assert cost_from_usage({}) == 0.0
    assert cost_from_usage({"cost": None}) == 0.0
    assert cost_from_usage({"cost": "garbage"}) == 0.0
    assert cost_from_usage({"cost": -1}) == 0.0


async def test_spend_accumulates_and_blocks_at_the_ceiling(db: AsyncSession) -> None:
    await ensure_budget(db, "openrouter", 0.01, JAN)  # nothing spent yet
    await record_spend(db, "openrouter", 0.004, JAN)
    await record_spend(db, "openrouter", 0.004, JAN)
    await db.commit()
    assert await month_spend(db, "openrouter", JAN) == pytest.approx(0.008)
    await ensure_budget(db, "openrouter", 0.01, JAN)  # still under
    await record_spend(db, "openrouter", 0.003, JAN)
    await db.commit()
    with pytest.raises(BudgetExceeded):
        await ensure_budget(db, "openrouter", 0.01, JAN)


async def test_zero_ceiling_blocks_everything(db: AsyncSession) -> None:
    with pytest.raises(BudgetExceeded):
        await ensure_budget(db, "openrouter", 0.0, JAN)


async def test_budget_is_per_month_and_per_provider(db: AsyncSession) -> None:
    await record_spend(db, "openrouter", 0.5, JAN)
    await db.commit()
    with pytest.raises(BudgetExceeded):
        await ensure_budget(db, "openrouter", 0.01, JAN)
    await ensure_budget(db, "openrouter", 0.01, FEB)  # new month, fresh budget
    await ensure_budget(db, "someone-else", 0.01, JAN)  # other provider unaffected


def test_paid_openrouter_model_is_rejected_by_default() -> None:
    validate_openrouter_model("meta-llama/llama-3.3-8b-instruct:free", allow_paid=False)
    with pytest.raises(ValueError):
        validate_openrouter_model("openai/gpt-4o", allow_paid=False)
    validate_openrouter_model("openai/gpt-4o", allow_paid=True)


def test_build_providers_refuses_a_paid_model() -> None:
    s = Settings(openrouter_api_key="k", openrouter_model="openai/gpt-4o")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_providers(s)
    ok = Settings(openrouter_api_key="k", openrouter_model="x/y:free")  # type: ignore[arg-type]
    assert any(p.provider == "openrouter" for p in build_providers(ok))


async def test_provider_free_only_guard_blocks_before_any_request() -> None:
    p = OpenAICompatProvider(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4o",
        locality="cloud",
        max_classification=Classification.public,
        api_key="k",
        free_only=True,
    )
    with pytest.raises(ProviderError, match="not a free model"):
        async for _ in p.stream_chat([{"role": "user", "content": "hi"}], classification=Classification.public):
            pass


# --- Daily request cap and per-provider ceilings (ADR 0024) ------------------------------------------------

DAY1 = datetime(2026, 1, 15, 10, tzinfo=UTC)
DAY2 = datetime(2026, 1, 16, 10, tzinfo=UTC)


async def test_daily_request_cap_counts_before_sending_and_resets_per_day(db: AsyncSession) -> None:
    for _ in range(2):
        await reserve_request(db, "acme-cloud", 2, DAY1)
    await db.commit()
    with pytest.raises(BudgetExceeded, match="Daily request limit"):
        await reserve_request(db, "acme-cloud", 2, DAY1)
    await db.commit()
    assert await requests_today(db, "acme-cloud", DAY1) == 3  # the refused attempt is visible, not sent
    await reserve_request(db, "acme-cloud", 2, DAY2)  # new day
    await reserve_request(db, "other", 2, DAY1)  # other provider
    for _ in range(3):
        await reserve_request(db, "uncapped", None, DAY1)  # no cap configured: counted, never refused
    assert await requests_today(db, "uncapped", DAY1) == 3
    with pytest.raises(BudgetExceeded):
        await reserve_request(db, "blocked", 0, DAY1)


def test_effective_limits_can_only_be_lowered() -> None:
    s = Settings(provider_monthly_budget_usd=0.01, provider_daily_request_cap=100, openrouter_monthly_budget_usd=0.02)
    api = LlmProvider(slug="acme", kind="api", builtin=False, monthly_budget_usd=None, daily_request_limit=None)
    assert registry.effective_budget(api, s) == 0.01 and registry.effective_daily_cap(api, s) == 100
    api.monthly_budget_usd, api.daily_request_limit = 5.0, 10_000  # tampered above the env: capped
    assert registry.effective_budget(api, s) == 0.01 and registry.effective_daily_cap(api, s) == 100
    api.monthly_budget_usd, api.daily_request_limit = 0.001, 3
    assert registry.effective_budget(api, s) == 0.001 and registry.effective_daily_cap(api, s) == 3
    router = LlmProvider(slug="openrouter", kind="api", builtin=True, monthly_budget_usd=None, daily_request_limit=None)
    assert registry.effective_budget(router, s) == 0.02 and registry.effective_daily_cap(router, s) is None
    router.daily_request_limit = 7
    assert registry.effective_daily_cap(router, s) == 7
    # Metering: every API provider (even self-hosted, it may proxy a paid API) and any non-local Ollama.
    assert registry.metered(api, "local") and registry.metered(api, "cloud")
    ollama = LlmProvider(slug="lan", kind="ollama", builtin=False)
    assert not registry.metered(ollama, "local") and registry.metered(ollama, "cloud")

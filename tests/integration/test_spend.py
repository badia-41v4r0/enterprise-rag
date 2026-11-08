"""Spend ceiling and free-only guard (ADR 0016). Real Postgres; no model is involved."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.providers.base import ProviderError
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.router import build_providers, validate_openrouter_model
from app.models import Classification
from app.services.spend import (
    BudgetExceeded,
    cost_from_usage,
    ensure_budget,
    month_spend,
    record_spend,
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

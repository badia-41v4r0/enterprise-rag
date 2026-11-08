"""Monthly spend tracking and the hard ceiling for cloud providers (ADR 0016)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.providers.base import ProviderError
from app.models import ProviderSpend


class BudgetExceeded(ProviderError):
    pass


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def cost_from_usage(usage: dict[str, Any]) -> float:
    """The cost OpenRouter reports in the final usage chunk (USD). Missing or invalid means 0."""
    try:
        return max(0.0, float(usage.get("cost") or 0.0))
    except (TypeError, ValueError):
        return 0.0


async def month_spend(db: AsyncSession, provider: str, now: datetime | None = None) -> float:
    row = await db.scalar(
        select(ProviderSpend.cost_usd).where(ProviderSpend.provider == provider, ProviderSpend.month == month_key(now))
    )
    return float(row or 0.0)


async def ensure_budget(db: AsyncSession, provider: str, ceiling_usd: float, now: datetime | None = None) -> None:
    """Refuse a request once the month's spend has reached the ceiling."""
    spent = await month_spend(db, provider, now)
    if spent >= ceiling_usd:
        raise BudgetExceeded(
            f"Monthly spend limit for {provider} reached (${spent:.4f} of ${ceiling_usd:.4f}); "
            "no more requests are sent to it this month."
        )


async def record_spend(db: AsyncSession, provider: str, cost_usd: float, now: datetime | None = None) -> None:
    """Add one request and its cost to the month's total (atomic upsert). The caller commits."""
    stmt = pg_insert(ProviderSpend).values(provider=provider, month=month_key(now), cost_usd=cost_usd, requests=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProviderSpend.provider, ProviderSpend.month],
        set_={
            "cost_usd": ProviderSpend.cost_usd + stmt.excluded.cost_usd,
            "requests": ProviderSpend.requests + 1,
        },
    )
    await db.execute(stmt)

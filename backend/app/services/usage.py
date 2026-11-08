"""Token accounting, prices, the preventive worst-case check and quotas (PLAN-v0.3 R6; ADR 0026, ADR 0027).

Order of checks for every model request (after routing has chosen a cleared provider):
per-request limits (input size, per-turn cap), provider and model budgets (worst case: estimated input
tokens x input price + max output tokens x output price), the user's quotas (their standard allowance and
every collective pool of every role they hold), then the daily request counter, then the egress hook and
the request itself. After the stream the real usage is recorded (reported tokens and cost when the provider
gives them, otherwise an estimate of characters / 4, flagged as estimated).

Only figures are stored: never a prompt, a question or an answer.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LlmUsage, Quota, QuotaPoolUsage, Role
from app.services.spend import BudgetExceeded, day_key, month_key, month_spend


class LimitReached(BudgetExceeded):
    """A provider or model limit (size, tokens, money) would be exceeded; nothing was sent."""

    code = "limit_reached"


class QuotaExceeded(BudgetExceeded):
    """The user's allowance or a pool of one of their roles is used up; nothing was sent."""

    code = "quota_exceeded"


# --- Limits per model (built from the registry) ------------------------------------------------------------


@dataclass(frozen=True)
class ModelLimits:
    provider: str
    model: str
    metered: bool = False
    max_output_tokens: int = 2048
    max_input_tokens: int | None = None
    provider_tokens_per_day: int | None = None
    provider_tokens_per_month: int | None = None
    model_tokens_per_day: int | None = None
    model_tokens_per_month: int | None = None
    provider_monthly_usd: float | None = None
    model_monthly_usd: float | None = None
    daily_requests: int | None = None
    max_turn_tokens: int | None = None
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    price_known: bool = False

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_usd_per_mtok + completion_tokens * self.output_usd_per_mtok) / 1_000_000

    def worst_case_usd(self, input_tokens: int) -> float:
        return self.cost(input_tokens, self.max_output_tokens)


def _chars(messages: list[dict[str, Any]], tools: list[Any] | None = None) -> int:
    n = len(json.dumps(messages, ensure_ascii=False))
    if tools:
        n += len(json.dumps([t.to_openai() if hasattr(t, "to_openai") else t for t in tools], ensure_ascii=False))
    return n


def preventive_input_tokens(messages: list[dict[str, Any]], tools: list[Any] | None = None) -> int:
    """Conservative input estimate for the check BEFORE sending: characters / 3 (tokenizers average about 4
    characters per token for English; the margin covers other languages and JSON)."""
    return math.ceil(_chars(messages, tools) / 3)


def estimated_tokens(text_chars: int) -> int:
    return math.ceil(text_chars / 4)


@dataclass
class Measured:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    estimated: bool

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def measure(
    usage: dict[str, Any],
    lim: ModelLimits,
    messages: list[dict[str, Any]],
    tools: list[Any] | None,
    output_chars: int,
) -> Measured:
    """Tokens and cost of one finished request: the provider's `usage` when present, else characters / 4."""
    estimated = False
    try:
        prompt = int(usage["prompt_tokens"])
        completion = int(usage.get("completion_tokens") or 0)
    except (KeyError, TypeError, ValueError):
        prompt = estimated_tokens(_chars(messages, tools))
        completion = estimated_tokens(output_chars)
        estimated = True
    prompt, completion = max(0, prompt), max(0, completion)
    reported = usage.get("cost")
    cost: float
    try:
        cost = max(0.0, float(reported)) if reported is not None else lim.cost(prompt, completion)
    except (TypeError, ValueError):
        cost = lim.cost(prompt, completion)
    return Measured(prompt, completion, cost, estimated)


# --- Usage sums ---------------------------------------------------------------------------------------------


@dataclass
class Totals:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    estimated_requests: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_ANY = object()


async def totals(
    db: AsyncSession,
    *,
    provider: str | None = None,
    model: str | None = None,
    user_id: uuid.UUID | object | None = _ANY,
    day: str | None = None,
    month: str | None = None,
) -> Totals:
    q = select(
        func.coalesce(func.sum(LlmUsage.requests), 0),
        func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
        func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
        func.coalesce(func.sum(LlmUsage.cost_usd), 0.0),
        func.coalesce(func.sum(LlmUsage.estimated_requests), 0),
    )
    if provider is not None:
        q = q.where(LlmUsage.provider == provider)
    if model is not None:
        q = q.where(LlmUsage.model == model)
    if user_id is not _ANY:
        q = q.where(LlmUsage.user_id.is_(None) if user_id is None else LlmUsage.user_id == user_id)
    if day is not None:
        q = q.where(LlmUsage.day == day)
    if month is not None:
        q = q.where(LlmUsage.month == month)
    r = (await db.execute(q)).one()
    return Totals(int(r[0]), int(r[1]), int(r[2]), float(r[3]), int(r[4]))


async def record_usage(
    db: AsyncSession,
    *,
    provider: str,
    model: str,
    user_id: uuid.UUID | None,
    measured: Measured,
    now: datetime | None = None,
) -> None:
    """Add one request to the (provider, model, user, day) row (atomic upsert). The caller commits."""
    ins = pg_insert(LlmUsage).values(
        provider=provider,
        model=model,
        user_id=user_id,
        day=day_key(now),
        month=month_key(now),
        requests=1,
        prompt_tokens=measured.prompt_tokens,
        completion_tokens=measured.completion_tokens,
        cost_usd=measured.cost_usd,
        estimated_requests=1 if measured.estimated else 0,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[LlmUsage.provider, LlmUsage.model, LlmUsage.user_id, LlmUsage.day],
        set_={
            "requests": LlmUsage.requests + 1,
            "prompt_tokens": LlmUsage.prompt_tokens + ins.excluded.prompt_tokens,
            "completion_tokens": LlmUsage.completion_tokens + ins.excluded.completion_tokens,
            "cost_usd": LlmUsage.cost_usd + ins.excluded.cost_usd,
            "estimated_requests": LlmUsage.estimated_requests + ins.excluded.estimated_requests,
        },
    )
    await db.execute(stmt)


# --- Quotas (ADR 0027) --------------------------------------------------------------------------------------

DIMENSIONS = ("tokens_per_day", "tokens_per_month", "usd_per_day", "usd_per_month")


@dataclass
class Cap:
    """One applicable allowance: the user's standard allowance (for one provider scope) or a role pool."""

    kind: str  # "standard" | "collective"
    provider: str | None
    limits: dict[str, float | None]
    source: str  # "user override" | "role <name>" | "roles <a>, <b>" | pool role name
    quota_id: uuid.UUID | None = None  # collective pools only
    role_name: str | None = None
    used: dict[str, float] = field(default_factory=dict)

    def label(self, period: str) -> str:
        scope = f" for {self.provider}" if self.provider else ""
        when = "daily" if period == "day" else "monthly"
        if self.kind == "collective":
            return f"The shared {when} allowance of the {self.role_name} role{scope}"
        return f"Your {when} allowance{scope}"


def _merge_standard(rows: list[Quota]) -> dict[str, float | None]:
    """The highest standard cap among a person's roles, per dimension; a role without a value in a dimension
    gives no limit there (so it wins)."""
    out: dict[str, float | None] = {}
    for dim in DIMENSIONS:
        values = [getattr(r, dim) for r in rows]
        out[dim] = None if any(v is None for v in values) else max(float(v) for v in values)
    return out


async def applicable_caps(
    db: AsyncSession, user_id: uuid.UUID, role_ids: frozenset[uuid.UUID] | set[uuid.UUID], provider: str | None
) -> list[Cap]:
    """Every cap that applies to a request by this user to this provider.

    Per provider scope (all providers, and this provider): the standard allowance is the user's override if
    one exists, otherwise the highest standard cap among their roles. Every collective pool of every role they
    hold (for all providers or for this provider) is a cap of its own."""
    conds = [Quota.user_id == user_id]
    if role_ids:
        conds.append(Quota.role_id.in_(list(role_ids)))
    rows = list(
        (
            await db.scalars(
                select(Quota).where(
                    or_(*conds),
                    Quota.provider.is_(None)
                    if provider is None
                    else or_(Quota.provider.is_(None), Quota.provider == provider),
                )
            )
        ).all()
    )
    if not rows:
        return []
    ids = [r.role_id for r in rows if r.role_id]
    role_names: dict[uuid.UUID | None, str] = {
        rid: name for rid, name in (await db.execute(select(Role.id, Role.name).where(Role.id.in_(ids)))).all()
    }
    caps: list[Cap] = []
    for scope in (None,) if provider is None else (None, provider):
        mine = [r for r in rows if r.subject == "user" and r.provider == scope]
        roles = [r for r in rows if r.subject == "role" and r.kind == "standard" and r.provider == scope]
        if mine:
            caps.append(Cap("standard", scope, {d: getattr(mine[0], d) for d in DIMENSIONS}, "user override"))
        elif roles:
            names = ", ".join(sorted(str(role_names.get(r.role_id, "?")) for r in roles))
            caps.append(Cap("standard", scope, _merge_standard(roles), f"roles {names}"))
    for r in rows:
        if r.subject == "role" and r.kind == "collective":
            name = str(role_names.get(r.role_id, "?"))
            caps.append(
                Cap(
                    "collective",
                    r.provider,
                    {d: getattr(r, d) for d in DIMENSIONS},
                    f"pool of role {name}",
                    quota_id=r.id,
                    role_name=name,
                )
            )
    return [c for c in caps if any(v is not None for v in c.limits.values())]


async def pool_used(db: AsyncSession, quota_id: uuid.UUID, period: str) -> tuple[int, float]:
    row = (
        await db.execute(
            select(QuotaPoolUsage.tokens, QuotaPoolUsage.cost_usd).where(
                QuotaPoolUsage.quota_id == quota_id, QuotaPoolUsage.period == period
            )
        )
    ).one_or_none()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)


async def cap_usage(db: AsyncSession, cap: Cap, user_id: uuid.UUID, now: datetime | None = None) -> dict[str, float]:
    day, month = day_key(now), month_key(now)
    if cap.kind == "collective":
        assert cap.quota_id is not None
        dt, dc = await pool_used(db, cap.quota_id, day)
        mt, mc = await pool_used(db, cap.quota_id, month)
    else:
        d = await totals(db, provider=cap.provider, user_id=user_id, day=day)
        m = await totals(db, provider=cap.provider, user_id=user_id, month=month)
        dt, dc, mt, mc = d.tokens, d.cost_usd, m.tokens, m.cost_usd
    cap.used = {"tokens_per_day": dt, "tokens_per_month": mt, "usd_per_day": dc, "usd_per_month": mc}
    return cap.used


def _reset(period: str) -> str:
    return "it resets at 00:00 UTC" if period == "day" else "it resets on the 1st of next month at 00:00 UTC"


async def check_quotas(
    db: AsyncSession,
    caps: list[Cap],
    user_id: uuid.UUID,
    worst_tokens: int,
    worst_usd: float,
    now: datetime | None = None,
) -> None:
    """Refuse when the worst case of this request would exceed any applicable cap. The message names the
    allowance and when it resets, and never shows anyone else's usage."""
    for cap in caps:
        used = await cap_usage(db, cap, user_id, now)
        for dim, add in (
            ("tokens_per_day", worst_tokens),
            ("tokens_per_month", worst_tokens),
            ("usd_per_day", worst_usd),
            ("usd_per_month", worst_usd),
        ):
            limit = cap.limits.get(dim)
            if limit is None:
                continue
            period = "day" if dim.endswith("day") else "month"
            if used[dim] >= limit or used[dim] + add > limit:
                raise QuotaExceeded(f"{cap.label(period)} is used up; {_reset(period)}.")


async def charge_pools(
    db: AsyncSession, caps: list[Cap], tokens: int, cost_usd: float, now: datetime | None = None
) -> None:
    """Charge one finished request to every collective pool that applied to it (day and month rows)."""
    for cap in caps:
        if cap.kind != "collective" or cap.quota_id is None:
            continue
        for period in (day_key(now), month_key(now)):
            ins = pg_insert(QuotaPoolUsage).values(
                quota_id=cap.quota_id, period=period, tokens=tokens, cost_usd=cost_usd
            )
            await db.execute(
                ins.on_conflict_do_update(
                    index_elements=[QuotaPoolUsage.quota_id, QuotaPoolUsage.period],
                    set_={
                        "tokens": QuotaPoolUsage.tokens + ins.excluded.tokens,
                        "cost_usd": QuotaPoolUsage.cost_usd + ins.excluded.cost_usd,
                    },
                )
            )


# --- The preventive check -----------------------------------------------------------------------------------


async def check_limits(
    db: AsyncSession,
    lim: ModelLimits,
    input_tokens: int,
    *,
    turn_tokens: int = 0,
    now: datetime | None = None,
) -> None:
    """Provider and model limits for one request, on its worst case. Raises LimitReached; sends nothing."""
    out = lim.max_output_tokens
    worst_tokens = input_tokens + out
    who = lim.model_id
    if lim.max_input_tokens is not None and input_tokens > lim.max_input_tokens:
        raise LimitReached(
            f"The request for {who} is too large (about {input_tokens} input tokens; the limit is "
            f"{lim.max_input_tokens} per request)."
        )
    if lim.max_turn_tokens is not None and turn_tokens + worst_tokens > lim.max_turn_tokens:
        raise LimitReached(
            f"The per-turn token limit of {lim.provider} ({lim.max_turn_tokens} tokens across the tool loop) "
            "would be exceeded; the answer stops here."
        )
    worst_usd = lim.worst_case_usd(input_tokens)
    day, month = day_key(now), month_key(now)
    if lim.provider_monthly_usd is not None:
        spent = await month_spend(db, lim.provider, now)
        if spent >= lim.provider_monthly_usd or spent + worst_usd > lim.provider_monthly_usd:
            raise LimitReached(
                f"Monthly spend limit for {lim.provider} reached (limit ${lim.provider_monthly_usd:.4f}; this "
                "request could cost more than what remains); no more requests are sent to it this month."
            )
    if lim.model_monthly_usd is not None:
        spent = (await totals(db, provider=lim.provider, model=lim.model, month=month)).cost_usd
        if spent >= lim.model_monthly_usd or spent + worst_usd > lim.model_monthly_usd:
            raise LimitReached(f"Monthly spend limit for {who} reached (limit ${lim.model_monthly_usd:.4f}).")
    for limit, scope, model, period in (
        (lim.provider_tokens_per_day, lim.provider, None, "day"),
        (lim.provider_tokens_per_month, lim.provider, None, "month"),
        (lim.model_tokens_per_day, who, lim.model, "day"),
        (lim.model_tokens_per_month, who, lim.model, "month"),
    ):
        if limit is None:
            continue
        t = await totals(
            db,
            provider=lim.provider,
            model=model,
            day=day if period == "day" else None,
            month=month if period == "month" else None,
        )
        if t.tokens >= limit or t.tokens + worst_tokens > limit:
            when = "today" if period == "day" else "this month"
            raise LimitReached(f"The token limit of {scope} for {when} ({limit} tokens) is reached.")


@dataclass
class Preflight:
    input_tokens: int
    caps: list[Cap]


async def preflight(
    db: AsyncSession,
    lim: ModelLimits,
    *,
    user_id: uuid.UUID | None,
    role_ids: frozenset[uuid.UUID] | set[uuid.UUID],
    input_tokens: int,
    turn_tokens: int = 0,
    now: datetime | None = None,
) -> Preflight:
    """Provider/model limits, then the user's quotas, on the worst case of this request."""
    await check_limits(db, lim, input_tokens, turn_tokens=turn_tokens, now=now)
    caps: list[Cap] = []
    if user_id is not None:
        caps = await applicable_caps(db, user_id, role_ids, lim.provider)
        await check_quotas(
            db, caps, user_id, input_tokens + lim.max_output_tokens, lim.worst_case_usd(input_tokens), now
        )
    return Preflight(input_tokens, caps)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def daily_series(
    db: AsyncSession, *, provider: str | None = None, user_id: uuid.UUID | None = None, days: int = 30
) -> list[dict[str, Any]]:
    """Requests, tokens and cost per UTC day for the last `days` days, zero-filled (figures only)."""
    from datetime import timedelta

    today = utcnow().date()
    keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    q = select(
        LlmUsage.day,
        func.sum(LlmUsage.requests),
        func.sum(LlmUsage.prompt_tokens + LlmUsage.completion_tokens),
        func.sum(LlmUsage.cost_usd),
    ).where(LlmUsage.day >= keys[0])
    if provider is not None:
        q = q.where(LlmUsage.provider == provider)
    if user_id is not None:
        q = q.where(LlmUsage.user_id == user_id)
    rows = {r[0]: r for r in (await db.execute(q.group_by(LlmUsage.day))).all()}
    return [
        {
            "day": k,
            "requests": int(rows[k][1]) if k in rows else 0,
            "tokens": int(rows[k][2]) if k in rows else 0,
            "cost_usd": float(rows[k][3]) if k in rows else 0.0,
        }
        for k in keys
    ]

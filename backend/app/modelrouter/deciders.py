"""Configured deciders: clearance from locality, budgets, the egress guard and one call (ADR 0030, 0031, 0032).

- `prompted`: a model of the provider registry answers in strict JSON. Its locality and clearance are those
  of that provider (further lowered by the decider's own clearance), and the provider's limits apply.
- `protocol`: an open Jev-style server speaking `POST /v1/systemone` (for example Open-Jev on this machine).
- `jev_official`: TypeSafe's hosted Jev API (default `https://api.typesafe.ai/v1/systemone`, model `jev-latest`).

Locality is derived from the address as for providers (ADR 0023/0025): private addresses only for deciders
marked self-hosted (created with step-up); a non-local decider is never cleared above
min(internal, ERAG_CLOUD_MAX_CLASSIFICATION) and starts at `public`. A decider only ever receives the user's
question and metadata; retrieved context only when it is local AND the admin enabled "see context".
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.netguard import BlockedDestination, UnresolvableHost, check_provider_url
from app.llm import registry
from app.llm.providers.base import Done, LLMProvider, ProviderError, TextDelta
from app.llm.router import ModelRouter
from app.modelrouter import prompted, protocol
from app.modelrouter.decision import Answers, DeciderError, Meta
from app.models import Classification, RouterDecider
from app.services import vault
from app.services.audit import audit
from app.services.spend import BudgetExceeded, month_spend, record_spend, reserve_request
from app.services.usage import ModelLimits, check_limits, measure, record_usage

KINDS = ("prompted", "protocol", "jev_official")
OFFICIAL_URL = "https://api.typesafe.ai/v1/systemone"
OFFICIAL_MODEL = "jev-latest"
# Third-party reports, not TypeSafe's own documentation (which only says output tokens are currently free):
# a default so that the worst-case check has a figure; the admin can change it after the first real call.
OFFICIAL_INPUT_USD_PER_MTOK = 0.042
PROTOCOL_OUTPUT_ESTIMATE = 64  # tokens, for the worst case of a protocol call


def secret_name_for(row: RouterDecider) -> str:
    return f"decider.{row.id.hex[:12]}.api_key"


def usage_key(row: RouterDecider) -> str:
    """The key under which a decider's requests and spend are counted (provider_daily_usage, provider_spend)."""
    return f"decider:{row.id.hex[:24]}"


def clearance_for(
    requested: Classification | None, locality: str, s: Settings, provider_clearance: Classification | None = None
) -> Classification:
    """Capped by locality (a non-local decider is never cleared for confidential) and, for a prompted decider,
    by its model's clearance. Default: `public` when not local, the cap when local."""
    cap = registry.locality_cap(locality, s)
    if provider_clearance is not None and provider_clearance.rank < cap.rank:
        cap = provider_clearance
    if requested is None:
        requested = Classification.public if locality != "local" else cap
    return requested if requested.rank <= cap.rank else cap


def caps_for(row: RouterDecider, locality: str, s: Settings) -> tuple[float | None, int | None]:
    """Monthly USD and daily requests of the decider itself: environment defaults for non-local deciders."""
    if locality == "local":
        return row.monthly_budget_usd, row.daily_request_cap
    monthly = s.provider_monthly_budget_usd if row.monthly_budget_usd is None else row.monthly_budget_usd
    daily = s.provider_daily_request_cap if row.daily_request_cap is None else row.daily_request_cap
    return monthly, daily


def prices_for(row: RouterDecider) -> tuple[float, float]:
    if row.kind == "jev_official" and row.input_usd_per_mtok is None and row.output_usd_per_mtok is None:
        return OFFICIAL_INPUT_USD_PER_MTOK, 0.0
    return float(row.input_usd_per_mtok or 0.0), float(row.output_usd_per_mtok or 0.0)


@dataclass
class Resolved:
    row: RouterDecider
    locality: str
    clearance: Classification
    available: bool
    problem: str = ""
    url: str | None = None
    provider: LLMProvider | None = None
    limits: ModelLimits | None = None

    @property
    def name(self) -> str:
        return self.row.name


async def resolve(row: RouterDecider, router: ModelRouter, s: Settings | None = None) -> Resolved:
    """Where the decider is (locality from the address), its clearance and whether it can be used now."""
    s = s or get_settings()
    if row.kind == "prompted":
        provider = router.get(row.model or "")
        if provider is None:
            return Resolved(row, "cloud", Classification.public, False, f"model {row.model} is not available")
        clearance = clearance_for(row.max_classification, provider.locality, s, provider.max_classification)
        return Resolved(row, provider.locality, clearance, True, provider=provider, limits=router.limits_for(provider))
    if not row.base_url:
        return Resolved(row, "cloud", Classification.public, False, "no endpoint URL")
    url = protocol.endpoint(row.base_url)
    try:
        ep = await check_provider_url(url, allow_private=row.self_hosted)
    except UnresolvableHost as exc:
        return Resolved(row, "cloud", Classification.public, False, str(exc), url=url)
    except BlockedDestination as exc:
        return Resolved(row, "cloud", Classification.public, False, f"destination refused: {exc}", url=url)
    if ep.locality == "cloud" and ep.target.scheme != "https":
        return Resolved(row, "cloud", Classification.public, False, "a public endpoint must use https", url=url)
    return Resolved(row, ep.locality, clearance_for(row.max_classification, ep.locality, s), True, url=url)


@dataclass
class Outcome:
    answers: Answers
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str | None


def _est_tokens(text: str) -> int:
    return math.ceil(len(text) / 3)


async def _check_budget(db: AsyncSession, res: Resolved, worst_usd: float, s: Settings) -> None:
    monthly, daily = caps_for(res.row, res.locality, s)
    key = usage_key(res.row)
    if monthly is not None:
        spent = await month_spend(db, key)
        if spent >= monthly or spent + worst_usd > monthly:
            raise DeciderError(f"monthly budget of decider {res.name} reached")
    try:
        await reserve_request(db, key, daily)
    except BudgetExceeded as exc:
        await db.commit()
        raise DeciderError(f"daily request cap of decider {res.name} reached") from exc
    await db.commit()


async def call(
    db: AsyncSession,
    res: Resolved,
    question: str,
    meta: Meta,
    *,
    context: str | None,
    timeout_s: float,
    user_id: Any = None,
    s: Settings | None = None,
) -> Outcome:
    """One decision. Clearance first: a decider not cleared for the classification is never called."""
    s = s or get_settings()
    if not res.available:
        raise DeciderError(res.problem or "unavailable")
    if meta.classification.rank > res.clearance.rank:
        raise DeciderError(f"not cleared for {meta.classification.value} data (cleared for {res.clearance.value})")
    if res.locality != "local" or not res.row.see_context:
        context = None  # only a local decider whose admin enabled "see context" ever receives retrieved text
    audit(
        db,
        "router.decider.request",
        user_id=user_id,
        resource=f"decider:{res.name}",
        details={
            "kind": res.row.kind,
            "locality": res.locality,
            "classification": meta.classification.value,
            "context_sent": context is not None,
        },
    )
    if res.row.kind == "prompted":
        return await _call_prompted(db, res, question, meta, context, s)
    return await _call_protocol(db, res, question, meta, context, timeout_s)


async def _call_prompted(
    db: AsyncSession, res: Resolved, question: str, meta: Meta, context: str | None, s: Settings
) -> Outcome:
    assert res.provider is not None and res.limits is not None
    messages = prompted.build_messages(question, meta, context)
    lim = dataclasses.replace(res.limits, max_output_tokens=prompted.MAX_OUTPUT_TOKENS)
    est = _est_tokens(str(messages))
    await _check_budget(db, res, lim.worst_case_usd(est), s)
    try:
        await check_limits(db, lim, est)  # the underlying provider's own budgets apply to router calls too
        if lim.metered or lim.daily_requests is not None:
            await reserve_request(db, res.provider.provider, lim.daily_requests)
    except BudgetExceeded as exc:
        await db.commit()
        raise DeciderError(f"model {res.provider.model_id}: {exc}") from exc
    await db.commit()
    text, usage = "", {}
    try:
        async for ev in res.provider.stream_chat(
            messages, classification=meta.classification, max_tokens=prompted.MAX_OUTPUT_TOKENS
        ):
            if isinstance(ev, TextDelta):
                text += ev.text
            elif isinstance(ev, Done):
                usage = ev.usage
    except ProviderError as exc:
        raise DeciderError(f"model {res.provider.model_id} failed: {exc}") from exc
    used = measure(usage, lim, messages, None, len(text))
    await record_usage(db, provider=res.provider.provider, model=res.provider.model, user_id=None, measured=used)
    if lim.metered or lim.provider_monthly_usd is not None:
        await record_spend(db, res.provider.provider, used.cost_usd)
    await record_spend(db, usage_key(res.row), used.cost_usd)
    await db.commit()
    return Outcome(
        prompted.parse_output(text), used.cost_usd, used.prompt_tokens, used.completion_tokens, res.provider.model_id
    )


async def _call_protocol(
    db: AsyncSession, res: Resolved, question: str, meta: Meta, context: str | None, timeout_s: float
) -> Outcome:
    assert res.url is not None
    model = res.row.model or (OFFICIAL_MODEL if res.row.kind == "jev_official" else None)
    payload = protocol.build_request(question, meta, model=model, context=context)
    pin, pout = prices_for(res.row)
    est = _est_tokens(payload["state"]) + 300  # the questions and their criteria
    await _check_budget(db, res, (est * pin + PROTOCOL_OUTPUT_ESTIMATE * pout) / 1e6, get_settings())
    key: str | None = None
    if res.row.secret_name and await vault.exists(db, res.row.secret_name):
        key = await vault.reveal(db, res.row.secret_name, consumer=f"decider:{res.name}")
        await db.commit()
    data = await protocol.post(
        res.url,
        payload,
        api_key=key,
        allow_private=res.row.self_hosted,
        require_private=res.locality == "local",
        timeout_s=timeout_s,
    )
    answers, usage, answered_by = protocol.parse_response(data)
    cost = (usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6
    from app.services.usage import Measured

    await record_usage(
        db,
        provider=usage_key(res.row),
        model=answered_by or model or res.row.kind,
        user_id=None,
        measured=Measured(usage["input_tokens"], usage["output_tokens"], cost, usage["input_tokens"] == 0),
    )
    await record_spend(db, usage_key(res.row), cost)
    await db.commit()
    return Outcome(answers, cost, usage["input_tokens"], usage["output_tokens"], answered_by or model)

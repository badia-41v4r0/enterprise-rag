"""The model router (PLAN-v0.3 section 5, ADR 0029): classification first, optimization second.

1. Retrieval and the effective classification are computed as before (ADR 0003).
2. The permitted set is computed by existing code: models cleared for that classification (ModelRouter),
   enabled, within the provider/model budgets and the user's quotas, tool-capable when tools are offered.
3. A decider answers four questions; the answers select a lane; the lane's targets are filtered by the
   permitted set. Deciders are tried in order (a decider not cleared for the classification is skipped, as
   is one that fails, times out, is over budget or returns anything outside the schema); the deterministic
   rules decider is always last.
4. Fallbacks: no lane target permitted -> the default lane -> the default model -> the existing rules
   (`ModelRouter.choose`, exactly the behaviour with the router off).

The decision log stores classification, permitted set, decider attempts, answers, lane, chosen model, reason,
latency and cost: never the question or any context.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.providers.base import LLMProvider
from app.llm.router import ModelRouter
from app.modelrouter import deciders
from app.modelrouter.decision import Answers, DeciderError, Meta, lane_matches, rules_decide
from app.models import AppSetting, Classification, RouterDecider, RouterDecision, RouterLane
from app.services.spend import BudgetExceeded, requests_today
from app.services.usage import preflight

ROUTER_SETTING = "router"
DEFAULTS: dict[str, Any] = {"enabled": False, "timeout_ms": 3000, "long_context_tokens": 2000}


@dataclass
class RouterConfig:
    enabled: bool = False
    timeout_ms: int = 3000
    long_context_tokens: int = 2000


async def load_config(db: AsyncSession) -> RouterConfig:
    row = await db.get(AppSetting, ROUTER_SETTING, populate_existing=True)
    v = {**DEFAULTS, **(row.value if row is not None else {})}
    return RouterConfig(bool(v["enabled"]), int(v["timeout_ms"]), int(v["long_context_tokens"]))


@dataclass
class RouteResult:
    provider: LLMProvider | None
    permitted: list[str]
    decider: str
    answers: Answers | None
    lane: str | None
    reason: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    decision_id: int | None = None

    def summary(self) -> dict[str, Any]:
        return {"decider": self.decider, "lane": self.lane, "reason": self.reason}


async def permitted_models(
    db: AsyncSession,
    router: ModelRouter,
    classification: Classification,
    *,
    tools_offered: bool,
    user_id: uuid.UUID | None,
    role_ids: frozenset[uuid.UUID] | set[uuid.UUID],
    input_tokens: int,
) -> list[LLMProvider]:
    """Cleared, enabled (the router only holds enabled models), within budgets and quotas, tool-capable when
    tools are offered. Read-only: nothing is reserved here."""
    out: list[LLMProvider] = []
    for p in router.permitted(classification):
        lim = router.limits_for(p)
        if tools_offered and not lim.supports_tools:
            continue
        try:
            await preflight(db, lim, user_id=user_id, role_ids=role_ids, input_tokens=input_tokens)
        except BudgetExceeded:
            continue
        if lim.daily_requests is not None and await requests_today(db, p.provider) >= lim.daily_requests:
            continue
        out.append(p)
    return out


async def _deciders(db: AsyncSession) -> list[RouterDecider]:
    q = select(RouterDecider).where(RouterDecider.enabled).order_by(RouterDecider.position, RouterDecider.name)
    return list((await db.scalars(q.execution_options(populate_existing=True))).all())


async def _lanes(db: AsyncSession) -> list[RouterLane]:
    q = select(RouterLane).where(RouterLane.enabled).order_by(RouterLane.position, RouterLane.name)
    return list((await db.scalars(q.execution_options(populate_existing=True))).all())


async def decide(
    db: AsyncSession,
    router: ModelRouter,
    question: str,
    meta: Meta,
    *,
    context: str | None,
    cfg: RouterConfig,
    user_id: uuid.UUID | None,
) -> tuple[str, Answers, list[dict[str, Any]], float]:
    """(decider name, answers, attempts, cost). Falls through the configured deciders to the rules."""
    attempts: list[dict[str, Any]] = []
    cost = 0.0
    s = get_settings()
    for row in await _deciders(db):
        res = await deciders.resolve(row, router, s)
        timeout = (row.timeout_ms or cfg.timeout_ms) / 1000
        started = time.monotonic()
        try:
            out = await asyncio.wait_for(
                deciders.call(db, res, question, meta, context=context, timeout_s=timeout, user_id=user_id, s=s),
                timeout=timeout,
            )
        except TimeoutError:
            attempts.append({"decider": row.name, "ok": False, "error": f"timed out after {timeout:.1f} s"})
            continue
        except DeciderError as exc:
            attempts.append({"decider": row.name, "ok": False, "error": str(exc)[:200]})
            continue
        ms = int((time.monotonic() - started) * 1000)
        cost += out.cost_usd
        attempts.append({"decider": row.name, "ok": True, "latency_ms": ms, "cost_usd": out.cost_usd})
        return row.name, out.answers, attempts, cost
    attempts.append({"decider": "rules", "ok": True})
    return "rules", rules_decide(question, meta, cfg.long_context_tokens), attempts, cost


async def route(
    db: AsyncSession,
    router: ModelRouter,
    *,
    question: str,
    classification: Classification,
    tools_offered: bool,
    context_tokens: int,
    context_text: str | None,
    input_tokens: int,
    user_id: uuid.UUID | None,
    role_ids: frozenset[uuid.UUID] | set[uuid.UUID],
    cfg: RouterConfig,
    dry_run: bool = False,
) -> RouteResult:
    started = time.monotonic()
    allowed = await permitted_models(
        db,
        router,
        classification,
        tools_offered=tools_offered,
        user_id=user_id,
        role_ids=role_ids,
        input_tokens=input_tokens,
    )
    permitted_ids = [p.model_id for p in allowed]
    by_id = {p.model_id: p for p in allowed}
    meta = Meta(classification, tools_offered, context_tokens)
    if not allowed:
        result = RouteResult(None, [], "none", None, None, "no permitted model: the existing rules decide")
    else:
        name, answers, attempts, cost = await decide(
            db, router, question, meta, context=context_text, cfg=cfg, user_id=user_id
        )
        chosen: LLMProvider | None = None
        lane_name: str | None = None
        reason = ""
        lanes = await _lanes(db)
        for lane in [ln for ln in lanes if not ln.is_default]:
            if not lane_matches(lane.conditions, answers, classification):
                continue
            target = next((by_id[t] for t in lane.targets if t in by_id), None)
            if target is not None:
                chosen, lane_name, reason = target, lane.name, f"lane {lane.name}"
                break
        if chosen is None:
            default_lane = next((ln for ln in lanes if ln.is_default), None)
            if default_lane is not None:
                target = next((by_id[t] for t in default_lane.targets if t in by_id), None)
                if target is not None:
                    chosen, lane_name, reason = target, default_lane.name, "default lane"
        if chosen is None and router.default_id in by_id:
            chosen, reason = by_id[router.default_id], "no lane target permitted: default model"
        if chosen is None:
            reason = "no lane target permitted: the existing rules decide"
        result = RouteResult(chosen, permitted_ids, name, answers, lane_name, reason, attempts, cost_usd=cost)
    # Invariant (prompt injection cannot widen the set): whatever is chosen is in the permitted set.
    if result.provider is not None and result.provider.model_id not in permitted_ids:  # pragma: no cover
        result.provider, result.reason = None, "chosen model outside the permitted set: the existing rules decide"
    result.latency_ms = int((time.monotonic() - started) * 1000)
    row = RouterDecision(
        dry_run=dry_run,
        classification=classification,
        tools_offered=tools_offered,
        permitted=permitted_ids,
        decider=result.decider,
        attempts=result.attempts,
        answers=result.answers.to_json() if result.answers else {},
        lane=result.lane,
        chosen_model=result.provider.model_id if result.provider else None,
        reason=result.reason,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
    )
    db.add(row)
    await db.commit()
    result.decision_id = row.id
    return result

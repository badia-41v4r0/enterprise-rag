"""Admin > Model router (PLAN-v0.3 section 5; ADR 0029 to 0032).

- Off by default; off means exactly the previous behaviour. Turning it off is an instant kill switch (admin
  session only); turning it on and every other change need step-up and are audited (`admin.router.*`).
- Deciders: create, edit, delete (step-up), order, test (a minimal real call with sample text). Keys go to
  the credential store (write-only). Locality comes from the address; clearance is capped by locality.
- Lanes: conditions on the decider's answers and ordered target models; one default lane.
- Dry run: the permitted set, the decider's answers, the chosen model and the reason, without generating an
  answer. With a non-local decider in play the caller must confirm the text is sample text.
- Decision log: never the question or any context.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import STEP_UP_REQUIRED, AuthContext, recently_authenticated, require_admin, require_step_up
from app.authz.policy import effective_permissions
from app.core.config import get_settings
from app.core.db import get_db
from app.core.netguard import BlockedDestination, UnresolvableHost, check_provider_url
from app.llm import registry
from app.llm.router import RoutingRefused, load_router
from app.modelrouter import deciders, protocol
from app.modelrouter import service as smart
from app.modelrouter.decision import DIFFICULTY_LEVELS, TASK_TYPES, DeciderError, Meta, validate_conditions
from app.models import Classification, LlmModel, LlmProvider, RouterDecider, RouterDecision, RouterLane, User
from app.services import vault
from app.services.audit import audit
from app.services.settings_store import put_setting
from app.services.spend import month_spend, requests_today

router = APIRouter(prefix="/api/admin/router", tags=["model-router"])

Kind = Literal["prompted", "protocol", "jev_official"]
SAMPLE_QUESTION = "What is the capital of France?"


def _422(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg)


# --- Settings -------------------------------------------------------------------------------------------


class SettingsIn(BaseModel):
    enabled: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=60_000)
    long_context_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


@router.put("/settings")
async def put_settings(
    body: SettingsIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    cfg = await smart.load_config(db)
    current = {"enabled": cfg.enabled, "timeout_ms": cfg.timeout_ms, "long_context_tokens": cfg.long_context_tokens}
    new = {**current, **body.model_dump(exclude_none=True)}
    changes = {k: {"from": current[k], "to": new[k]} for k in new if new[k] != current[k]}
    only_disabling = set(changes) == {"enabled"} and new["enabled"] is False
    # The kill switch works with an admin session alone; enabling and any other change need step-up.
    if changes and not only_disabling and not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)
    if changes:
        await put_setting(db, smart.ROUTER_SETTING, new, auth.user.id)
        audit(db, "admin.router.settings", user_id=auth.user.id, resource="router", details={"changes": changes})
        await db.commit()
    return new


# --- Overview -------------------------------------------------------------------------------------------


async def _decider_out(db: AsyncSession, row: RouterDecider, res: deciders.Resolved) -> dict[str, Any]:
    s = get_settings()
    monthly, daily = deciders.caps_for(row, res.locality, s)
    key = deciders.usage_key(row)
    pin, pout = deciders.prices_for(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "enabled": row.enabled,
        "position": row.position,
        "base_url": row.base_url,
        "endpoint": res.url,
        "model": row.model,
        "self_hosted": row.self_hosted,
        "locality": res.locality,
        "requested_classification": row.max_classification.value if row.max_classification else None,
        "max_classification": res.clearance.value,
        "locality_max_classification": registry.locality_cap(res.locality, s).value,
        "see_context": row.see_context,
        "timeout_ms": row.timeout_ms,
        "monthly_budget_usd": monthly,
        "daily_request_cap": daily,
        "input_usd_per_mtok": pin,
        "output_usd_per_mtok": pout,
        "price_is_default": row.kind == "jev_official" and row.input_usd_per_mtok is None,
        "spent_this_month_usd": await month_spend(db, key),
        "requests_today": await requests_today(db, key),
        "key_source": "vault" if row.secret_name and await vault.exists(db, row.secret_name) else "none",
        "available": res.available,
        "status_reason": res.problem,
    }


def _lane_out(lane: RouterLane) -> dict[str, Any]:
    return {
        "id": str(lane.id),
        "name": lane.name,
        "position": lane.position,
        "enabled": lane.enabled,
        "is_default": lane.is_default,
        "conditions": lane.conditions,
        "targets": lane.targets,
    }


@router.get("")
async def overview(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    cfg = await smart.load_config(db)
    mr = await load_router(db)
    rows = (await db.scalars(select(RouterDecider).order_by(RouterDecider.position, RouterDecider.name))).all()
    lanes = (await db.scalars(select(RouterLane).order_by(RouterLane.position, RouterLane.name))).all()
    return {
        "settings": {
            "enabled": cfg.enabled,
            "timeout_ms": cfg.timeout_ms,
            "long_context_tokens": cfg.long_context_tokens,
        },
        "deciders": [await _decider_out(db, r, await deciders.resolve(r, mr)) for r in rows],
        "lanes": [_lane_out(ln) for ln in lanes],
        "models": [
            {
                "id": m.id,
                "locality": m.locality,
                "max_classification": m.max_classification.value,
                "supports_tools": mr.limits_for(p).supports_tools,
                "is_default": m.is_default,
            }
            for m, p in zip(mr.models(), mr.providers, strict=True)
        ],
        "task_types": TASK_TYPES,
        "difficulty_levels": DIFFICULTY_LEVELS,
        "official": {"url": deciders.OFFICIAL_URL, "model": deciders.OFFICIAL_MODEL},
    }


# --- Deciders -------------------------------------------------------------------------------------------


class DeciderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Kind
    base_url: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=250)
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)  # write-only
    self_hosted: bool = False
    max_classification: Classification | None = None
    see_context: bool = False
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    daily_request_cap: int | None = Field(default=None, ge=0)
    input_usd_per_mtok: float | None = Field(default=None, ge=0)
    output_usd_per_mtok: float | None = Field(default=None, ge=0)
    position: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool = True


class DeciderPatch(BaseModel):
    """Fields left out are unchanged; explicit null resets to the default."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    position: int | None = Field(default=None, ge=0, le=1000)
    base_url: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=250)
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    clear_api_key: bool = False
    self_hosted: bool | None = None
    max_classification: Classification | None = None
    see_context: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    daily_request_cap: int | None = Field(default=None, ge=0)
    input_usd_per_mtok: float | None = Field(default=None, ge=0)
    output_usd_per_mtok: float | None = Field(default=None, ge=0)


async def _locality_of(row: RouterDecider, db: AsyncSession) -> str:
    """Save-time checks (raise 422): a prompted decider names a registry model; an endpoint passes the egress
    guard (private only when self-hosted, never metadata/reserved, HTTPS when public)."""
    s = get_settings()
    if row.kind == "prompted":
        if row.base_url:
            raise _422("a prompted decider uses a model of the provider registry, not a URL")
        slug, sep, model = (row.model or "").partition(":")
        found = (
            await db.scalar(select(LlmProvider).join(LlmModel).where(LlmProvider.slug == slug, LlmModel.model == model))
            if sep
            else None
        )
        if found is None:
            raise _422("model must be an existing registry model id, <provider>:<model>")
        mr = await load_router(db, s)
        p = mr.get(row.model or "")
        return p.locality if p is not None else found.locality
    if not row.base_url:
        raise _422("this kind of decider needs an endpoint URL")
    try:
        ep = await check_provider_url(protocol.endpoint(row.base_url), allow_private=row.self_hosted)
    except (BlockedDestination, UnresolvableHost) as exc:
        raise _422(f"URL refused: {exc}") from exc
    if ep.locality == "cloud" and ep.target.scheme != "https":
        raise _422("a decider on a public address must use https")
    return ep.locality


def _check_policy(row: RouterDecider, locality: str) -> None:
    cap = registry.locality_cap(locality, get_settings())
    if row.max_classification is not None and row.max_classification.rank > cap.rank:
        raise _422(
            f"A {locality} decider can be cleared for at most {cap.value} data "
            "(a decider that is not local is never cleared for confidential)"
        )
    if row.see_context and locality != "local":
        raise _422("only a local decider may see the retrieved context")


async def _row(db: AsyncSession, decider_id: uuid.UUID) -> RouterDecider:
    row = await db.get(RouterDecider, decider_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Decider not found")
    return row


def _public(row: RouterDecider) -> dict[str, Any]:
    fields = (
        "name",
        "kind",
        "enabled",
        "position",
        "base_url",
        "model",
        "self_hosted",
        "see_context",
        "timeout_ms",
        "monthly_budget_usd",
        "daily_request_cap",
        "input_usd_per_mtok",
        "output_usd_per_mtok",
        "locality",
    )
    out = {f: getattr(row, f) for f in fields}
    out["max_classification"] = row.max_classification.value if row.max_classification else None
    return out


@router.post("/deciders", status_code=201)
async def create_decider(
    body: DeciderIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if await db.scalar(select(RouterDecider.id).where(RouterDecider.name == body.name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A decider with this name exists")
    data = body.model_dump(exclude={"api_key"})
    if body.kind == "jev_official":
        data["base_url"] = body.base_url or deciders.OFFICIAL_URL
        data["model"] = body.model or deciders.OFFICIAL_MODEL
    if body.kind != "prompted":
        data["self_hosted"] = body.self_hosted
    else:
        data["self_hosted"] = False
    if data["position"] is None:
        last = await db.scalar(select(RouterDecider.position).order_by(RouterDecider.position.desc()).limit(1))
        data["position"] = (last or 0) + 10
    row = RouterDecider(**data, created_by=auth.user.id)
    row.id = uuid.uuid4()
    row.locality = await _locality_of(row, db)
    _check_policy(row, row.locality)
    if row.locality != "local" and row.max_classification is None:
        row.max_classification = Classification.public  # explicit: cloud deciders start at public
    if body.api_key is not None:
        if row.kind == "prompted":
            raise _422("a prompted decider uses its provider's key")
        row.secret_name = deciders.secret_name_for(row)
        await vault.set_secret(
            db, row.secret_name, body.api_key, user_id=auth.user.id, description=f"API key of router decider {row.name}"
        )
    db.add(row)
    audit(
        db,
        "admin.router.decider.create",
        user_id=auth.user.id,
        resource=f"decider:{row.name}",
        details={**_public(row), "api_key_set": body.api_key is not None},
    )
    await db.commit()
    mr = await load_router(db)
    return await _decider_out(db, row, await deciders.resolve(row, mr))


@router.patch("/deciders/{decider_id}")
async def update_decider(
    decider_id: uuid.UUID,
    body: DeciderPatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await _row(db, decider_id)
    before = _public(row)
    fields = body.model_fields_set - {"api_key", "clear_api_key"}
    for f in fields:
        value = getattr(body, f)
        if f in ("name", "enabled", "position", "self_hosted", "see_context") and value is None:
            continue
        setattr(row, f, value)
    if row.kind == "prompted":
        row.self_hosted = False
    if row.kind == "jev_official" and not row.base_url:
        row.base_url = deciders.OFFICIAL_URL
    old_locality = row.locality
    row.locality = await _locality_of(row, db)
    if row.locality != "local" and old_locality == "local" and "max_classification" not in fields:
        row.max_classification = Classification.public  # it just became non-local
    if row.locality != "local" and "see_context" not in fields:
        row.see_context = False
    _check_policy(row, row.locality)
    if body.api_key is not None:
        if row.kind == "prompted":
            raise _422("a prompted decider uses its provider's key")
        row.secret_name = row.secret_name or deciders.secret_name_for(row)
        await vault.set_secret(
            db, row.secret_name, body.api_key, user_id=auth.user.id, description=f"API key of router decider {row.name}"
        )
    elif body.clear_api_key and row.secret_name and await vault.exists(db, row.secret_name):
        await vault.delete_secret(db, row.secret_name, user_id=auth.user.id)
    after = _public(row)
    audit(
        db,
        "admin.router.decider.update",
        user_id=auth.user.id,
        resource=f"decider:{row.name}",
        details={
            "changes": {k: {"from": before[k], "to": after[k]} for k in after if after[k] != before[k]},
            "api_key_changed": body.api_key is not None,
            "api_key_cleared": body.clear_api_key,
        },
    )
    await db.commit()
    mr = await load_router(db)
    return await _decider_out(db, row, await deciders.resolve(row, mr))


@router.delete("/deciders/{decider_id}", status_code=204)
async def delete_decider(
    decider_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    row = await _row(db, decider_id)
    if row.secret_name and await vault.exists(db, row.secret_name):
        await vault.delete_secret(db, row.secret_name, user_id=auth.user.id)
    audit(db, "admin.router.decider.delete", user_id=auth.user.id, resource=f"decider:{row.name}", details=_public(row))
    await db.delete(row)
    await db.commit()


@router.post("/deciders/{decider_id}/test")
async def test_decider(
    decider_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """A minimal real call with a fixed sample question classified public: latency, usage, cost, answers.
    It is counted against the decider's budget like any call."""
    row = await _row(db, decider_id)
    mr = await load_router(db)
    res = await deciders.resolve(row, mr)
    meta = Meta(Classification.public, False, 0)
    started = time.monotonic()
    cfg = await smart.load_config(db)
    timeout = (row.timeout_ms or max(cfg.timeout_ms, 10_000)) / 1000
    try:
        out = await deciders.call(db, res, SAMPLE_QUESTION, meta, context=None, timeout_s=timeout, user_id=auth.user.id)
    except DeciderError as exc:
        result: dict[str, Any] = {"ok": False, "error": str(exc)[:300], "locality": res.locality}
    else:
        result = {
            "ok": True,
            "locality": res.locality,
            "model": out.model,
            "answers": out.answers.to_json(),
            "usage": {"input_tokens": out.input_tokens, "output_tokens": out.output_tokens},
            "cost_usd": out.cost_usd,
        }
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    audit(
        db,
        "admin.router.decider.test",
        user_id=auth.user.id,
        resource=f"decider:{row.name}",
        details={k: v for k, v in result.items() if k != "answers"},
    )
    await db.commit()
    return result


# --- Lanes ----------------------------------------------------------------------------------------------


class LaneIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    position: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool = True
    is_default: bool = False
    conditions: dict[str, Any] = Field(default_factory=dict)
    targets: list[str] = Field(min_length=1, max_length=20)


class LanePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    position: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None
    is_default: bool | None = None
    conditions: dict[str, Any] | None = None
    targets: list[str] | None = Field(default=None, min_length=1, max_length=20)


async def _check_targets(db: AsyncSession, targets: list[str]) -> list[str]:
    known = {
        f"{slug}:{model}" for slug, model in (await db.execute(select(LlmProvider.slug, LlmModel.model).join(LlmModel)))
    }
    unknown = [t for t in targets if t not in known]
    if unknown:
        raise _422(f"unknown model id(s): {', '.join(unknown)}")
    return list(dict.fromkeys(targets))


async def _lane(db: AsyncSession, lane_id: uuid.UUID) -> RouterLane:
    lane = await db.get(RouterLane, lane_id)
    if lane is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lane not found")
    return lane


async def _only_default(db: AsyncSession, keep: RouterLane) -> None:
    for other in (await db.scalars(select(RouterLane).where(RouterLane.is_default, RouterLane.id != keep.id))).all():
        other.is_default = False
    await db.flush()


@router.post("/lanes", status_code=201)
async def create_lane(
    body: LaneIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    if await db.scalar(select(RouterLane.id).where(RouterLane.name == body.name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A lane with this name exists")
    try:
        conditions = validate_conditions(body.conditions)
    except ValueError as exc:
        raise _422(str(exc)) from exc
    position = body.position
    if position is None:
        last = await db.scalar(select(RouterLane.position).order_by(RouterLane.position.desc()).limit(1))
        position = (last or 0) + 10
    lane = RouterLane(
        id=uuid.uuid4(),
        name=body.name,
        position=position,
        enabled=body.enabled,
        is_default=False,
        conditions=conditions,
        targets=await _check_targets(db, body.targets),
        created_by=auth.user.id,
    )
    db.add(lane)
    if body.is_default:
        await _only_default(db, lane)
        lane.is_default = True
    audit(db, "admin.router.lane.create", user_id=auth.user.id, resource=f"lane:{lane.name}", details=_lane_out(lane))
    await db.commit()
    return _lane_out(lane)


@router.patch("/lanes/{lane_id}")
async def update_lane(
    lane_id: uuid.UUID,
    body: LanePatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    lane = await _lane(db, lane_id)
    before = _lane_out(lane)
    if body.conditions is not None:
        try:
            lane.conditions = validate_conditions(body.conditions)
        except ValueError as exc:
            raise _422(str(exc)) from exc
    if body.targets is not None:
        lane.targets = await _check_targets(db, body.targets)
    for f in ("name", "position", "enabled"):
        if getattr(body, f) is not None:
            setattr(lane, f, getattr(body, f))
    if body.is_default is True:
        await _only_default(db, lane)
        lane.is_default = True
    elif body.is_default is False:
        lane.is_default = False
    after = _lane_out(lane)
    audit(
        db,
        "admin.router.lane.update",
        user_id=auth.user.id,
        resource=f"lane:{lane.name}",
        details={"changes": {k: {"from": before[k], "to": after[k]} for k in after if after[k] != before[k]}},
    )
    await db.commit()
    return after


@router.delete("/lanes/{lane_id}", status_code=204)
async def delete_lane(
    lane_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    lane = await _lane(db, lane_id)
    audit(db, "admin.router.lane.delete", user_id=auth.user.id, resource=f"lane:{lane.name}", details=_lane_out(lane))
    await db.delete(lane)
    await db.commit()


# --- Dry run and the decision log ------------------------------------------------------------------------


class DryRunIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    classification: Classification = Classification.public
    persona_user_id: uuid.UUID
    context_tokens: int = Field(default=0, ge=0, le=1_000_000)
    tools_offered: bool | None = None  # None: whatever the persona would be offered at that classification
    sample_text_confirmed: bool = False


@router.post("/dry-run")
async def dry_run(
    body: DryRunIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Which model the router would choose for this persona, without generating an answer. Logged as a dry run
    (without the text). A non-local decider that would receive the text requires `sample_text_confirmed`."""
    persona = await db.get(User, body.persona_user_id)
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    perms = await effective_permissions(db, persona)
    tools_offered = body.tools_offered
    if tools_offered is None:
        from app.api.chat import get_tool_runtime

        runtime = get_tool_runtime()
        tools_offered = bool(await runtime.visible_tools(db, perms, body.classification)) if runtime else False
    mr = await load_router(db)
    cfg = await smart.load_config(db)
    rows = (await db.scalars(select(RouterDecider).where(RouterDecider.enabled))).all()
    external = []
    for r in rows:
        res = await deciders.resolve(r, mr)
        if res.available and res.locality != "local" and body.classification.rank <= res.clearance.rank:
            external.append(r.name)
    if external and not body.sample_text_confirmed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"sample_text_required: the text would be sent to a decider outside this network ({', '.join(external)}). "
            "Use sample text only and confirm it.",
        )
    result = await smart.route(
        db,
        mr,
        question=body.question,
        classification=body.classification,
        tools_offered=tools_offered,
        context_tokens=body.context_tokens,
        context_text=None,
        input_tokens=len(body.question) // 3 + body.context_tokens + 300,
        user_id=persona.id,
        role_ids=perms.role_ids,
        cfg=cfg,
        dry_run=True,
    )
    fallback: str | None = None
    if result.provider is None:
        try:
            fallback = mr.choose(body.classification).model_id
        except RoutingRefused as exc:
            fallback = f"refused: {exc.reason}"
    audit(
        db,
        "admin.router.dry_run",
        user_id=auth.user.id,
        resource=f"user:{persona.email}",
        details={"decision_id": result.decision_id, "classification": body.classification.value},
    )
    await db.commit()
    return {
        "router_enabled": cfg.enabled,
        "tools_offered": tools_offered,
        "permitted": result.permitted,
        "decider": result.decider,
        "attempts": result.attempts,
        "answers": result.answers.to_json() if result.answers else None,
        "lane": result.lane,
        "chosen_model": result.provider.model_id if result.provider else None,
        "fallback_model": fallback,
        "reason": result.reason,
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "decision_id": result.decision_id,
    }


@router.get("/decisions")
async def decisions(
    limit: int = 100, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(select(RouterDecision).order_by(RouterDecision.id.desc()).limit(max(1, min(limit, 500))))
    ).all()
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(),
            "dry_run": r.dry_run,
            "classification": r.classification.value,
            "tools_offered": r.tools_offered,
            "permitted": r.permitted,
            "decider": r.decider,
            "attempts": r.attempts,
            "answers": r.answers,
            "lane": r.lane,
            "chosen_model": r.chosen_model,
            "reason": r.reason,
            "latency_ms": r.latency_ms,
            "cost_usd": r.cost_usd,
        }
        for r in rows
    ]

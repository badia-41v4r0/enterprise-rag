"""LLM provider registry (admin): add Ollama instances and OpenAI-compatible APIs, pick the default model.

Security properties (ADR 0023 to 0025):
- locality comes from the resolved address, never from the admin; clearance is capped by locality;
- API keys are write-only (stored in the credential store, never returned or logged);
- private addresses only for Ollama or explicitly self-hosted providers, re-checked on every request;
- step-up for creating, deleting, changing URL/key/clearance and changing the default; everything audited.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import STEP_UP_REQUIRED, AuthContext, recently_authenticated, require_admin, require_step_up
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.netguard import BlockedDestination
from app.llm import registry
from app.llm.router import invalidate_router_cache, load_router
from app.models import Classification, LlmModel, LlmProvider
from app.services import usage, vault
from app.services.audit import audit
from app.services.spend import day_key, month_key, month_spend, requests_today

router = APIRouter(prefix="/api/admin", tags=["providers"])

Kind = Literal["ollama", "api"]


class UsageFigures(BaseModel):
    requests: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    estimated_requests: int = 0


class ModelOut(BaseModel):
    id: uuid.UUID
    model_id: str
    model: str
    enabled: bool
    env_managed: bool
    is_default: bool
    # Per-model overrides (NULL = the provider's value) and price in USD per million tokens (NULL = unknown).
    max_output_tokens: int | None = None
    max_input_tokens: int | None = None
    tokens_per_day: int | None = None
    tokens_per_month: int | None = None
    monthly_budget_usd: float | None = None
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    price_source: str | None = None
    supports_tools: bool = True
    requested_classification: Classification | None = None  # per-model cap (lower only); null = provider's
    is_jev_router: bool = False
    jev_router: dict[str, Any] = Field(default_factory=dict)
    today: UsageFigures = Field(default_factory=UsageFigures)
    month: UsageFigures = Field(default_factory=UsageFigures)


class ProviderOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    kind: str
    builtin: bool
    enabled: bool
    base_url: str
    self_hosted: bool
    locality: str
    requested_classification: Classification | None
    max_classification: Classification
    locality_max_classification: Classification
    key_source: str  # "vault" | "env" | "none" (never the key)
    secret_name: str | None
    metered: bool
    monthly_budget_usd: float | None
    budget_cap_usd: float | None
    spent_this_month_usd: float
    daily_request_limit: int | None
    daily_cap_env: int | None
    requests_today: int
    # Token limits (NULL = not set; max output falls back to ERAG_PROVIDER_MAX_OUTPUT_TOKENS).
    max_output_tokens: int | None
    default_max_output_tokens: int
    max_input_tokens: int | None
    tokens_per_day: int | None
    tokens_per_month: int | None
    max_turn_tokens: int | None
    today: UsageFigures
    month: UsageFigures
    available: bool
    status_reason: str
    models: list[ModelOut]


class ProvidersOut(BaseModel):
    providers: list[ProviderOut]
    default_model_id: str | None
    cloud_max_classification: Classification


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=40)
    kind: Kind
    base_url: str = Field(min_length=1, max_length=1000)
    self_hosted: bool = False
    # Write-only: stored in the credential store as `provider.<slug>.api_key`, never returned.
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    max_classification: Classification | None = None
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    daily_request_limit: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_input_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_month: int | None = Field(default=None, ge=0)
    max_turn_tokens: int | None = Field(default=None, ge=1)
    models: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class ProviderPatch(BaseModel):
    """Fields left out are unchanged; an explicit null resets clearance/limits to their defaults."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=1000)
    self_hosted: bool | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    clear_api_key: bool = False
    max_classification: Classification | None = None
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    daily_request_limit: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_input_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_month: int | None = Field(default=None, ge=0)
    max_turn_tokens: int | None = Field(default=None, ge=1)


class ModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ModelPatch(BaseModel):
    """Fields left out are unchanged; an explicit null resets a limit or price to "not set"."""

    enabled: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_input_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_month: int | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    input_usd_per_mtok: float | None = Field(default=None, ge=0, le=100_000)
    output_usd_per_mtok: float | None = Field(default=None, ge=0, le=100_000)
    supports_tools: bool | None = None
    max_classification: Classification | None = None  # null = the provider's clearance
    jev_router: dict[str, Any] | None = None  # OpenRouter Jev Router options (cost_tier, allowed/excluded)


PROVIDER_LIMITS = (
    "monthly_budget_usd",
    "daily_request_limit",
    "max_output_tokens",
    "max_input_tokens",
    "tokens_per_day",
    "tokens_per_month",
    "max_turn_tokens",
)
MODEL_LIMITS = ("max_output_tokens", "max_input_tokens", "tokens_per_day", "tokens_per_month", "monthly_budget_usd")
MODEL_PRICES = ("input_usd_per_mtok", "output_usd_per_mtok")


class DefaultIn(BaseModel):
    model_id: str | None = Field(default=None, max_length=250)  # "<slug>:<model>"; null = automatic


# Step-up (ADR 0019) for anything that opens a destination, raises what may be sent, or changes a limit (D2:
# limits may be raised, so every limit change is treated as sensitive and kept in the change history).
SENSITIVE_PATCH = {"base_url", "self_hosted", "api_key", "clear_api_key", "max_classification", *PROVIDER_LIMITS}


def _422(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "provider"


async def _row_or_404(db: AsyncSession, provider_id: uuid.UUID, *, fresh: bool = False) -> LlmProvider:
    row = await db.get(LlmProvider, provider_id, populate_existing=fresh)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    return row


def _env_defaults(row: LlmProvider, s: Settings) -> dict[str, Any]:
    return {
        "monthly_budget_usd": registry.budget_cap(row, s),
        "daily_request_limit": registry.daily_cap_env(row, s),
        "max_output_tokens": s.provider_max_output_tokens,
    }


def _check_clearance(requested: Classification | None, locality: str, s: Settings) -> None:
    cap = registry.locality_cap(locality, s)
    if requested is not None and requested.rank > cap.rank:
        raise _422(
            f"A {locality} provider can be cleared for at most {cap.value} data "
            "(confidential data is only ever processed by local models)"
        )


async def _probe(base_url: str, kind: str, self_hosted: bool) -> str:
    try:
        return (await registry.probe(base_url, kind, self_hosted)).locality
    except BlockedDestination as exc:
        raise _422(f"URL refused: {exc}") from exc


async def _out(
    db: AsyncSession, row: LlmProvider, s: Settings, states: dict[str, registry.ProviderState]
) -> ProviderOut:
    _key, source = await registry.resolve_key(db, row, s, reveal=False)
    state = states.get(row.slug)
    locality = state.locality if state and state.locality != "blocked" else row.locality
    is_metered = registry.metered(row, locality)
    return ProviderOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        kind=row.kind,
        builtin=row.builtin,
        enabled=row.enabled,
        base_url=row.base_url,
        self_hosted=row.self_hosted,
        locality=locality,
        requested_classification=row.max_classification,
        max_classification=registry.effective_clearance(row.max_classification, locality, s),
        locality_max_classification=registry.locality_cap(locality, s),
        key_source=source,
        secret_name=row.secret_name,
        metered=is_metered,
        monthly_budget_usd=registry.effective_budget(row, s) if is_metered else None,
        budget_cap_usd=registry.budget_cap(row, s) if is_metered else None,
        spent_this_month_usd=await month_spend(db, row.slug),
        daily_request_limit=registry.effective_daily_cap(row, s) if is_metered else None,
        daily_cap_env=registry.daily_cap_env(row, s) if is_metered else None,
        requests_today=await requests_today(db, row.slug),
        max_output_tokens=row.max_output_tokens,
        default_max_output_tokens=s.provider_max_output_tokens,
        max_input_tokens=row.max_input_tokens,
        tokens_per_day=row.tokens_per_day,
        tokens_per_month=row.tokens_per_month,
        max_turn_tokens=row.max_turn_tokens,
        today=_figures(await usage.totals(db, provider=row.slug, day=day_key())),
        month=_figures(await usage.totals(db, provider=row.slug, month=month_key())),
        available=bool(state and state.available),
        status_reason=(state.reason if state else "") or ("" if state and state.available else "not loaded"),
        models=[await _model_out(db, row.slug, m) for m in row.models],
    )


def _figures(t: usage.Totals) -> UsageFigures:
    return UsageFigures(
        requests=t.requests, tokens=t.tokens, cost_usd=round(t.cost_usd, 8), estimated_requests=t.estimated_requests
    )


async def _model_out(db: AsyncSession, slug: str, m: LlmModel) -> ModelOut:
    return ModelOut(
        id=m.id,
        model_id=f"{slug}:{m.model}",
        model=m.model,
        enabled=m.enabled,
        env_managed=m.env_managed,
        is_default=m.is_default,
        max_output_tokens=m.max_output_tokens,
        max_input_tokens=m.max_input_tokens,
        tokens_per_day=m.tokens_per_day,
        tokens_per_month=m.tokens_per_month,
        monthly_budget_usd=m.monthly_budget_usd,
        input_usd_per_mtok=m.input_usd_per_mtok,
        output_usd_per_mtok=m.output_usd_per_mtok,
        price_source=m.price_source,
        supports_tools=m.supports_tools,
        requested_classification=m.max_classification,
        is_jev_router=m.model == registry.JEV_ROUTER_MODEL and slug == registry.OPENROUTER,
        jev_router=dict((m.options or {}).get("jev_router") or {}),
        today=_figures(await usage.totals(db, provider=slug, model=m.model, day=day_key())),
        month=_figures(await usage.totals(db, provider=slug, model=m.model, month=month_key())),
    )


async def _default_model_id(db: AsyncSession) -> str | None:
    m = await db.scalar(select(LlmModel).where(LlmModel.is_default))
    return f"{m.provider.slug}:{m.model}" if m is not None else None


async def _changed(db: AsyncSession) -> None:
    await db.commit()
    invalidate_router_cache()


# --- Providers --------------------------------------------------------------------------------------


@router.get("/providers")
async def list_providers(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> ProvidersOut:
    s = get_settings()
    r = await load_router(db, s)  # creates the built-in rows if needed and evaluates every provider
    rows = (await db.scalars(select(LlmProvider).order_by(LlmProvider.builtin.desc(), LlmProvider.name))).all()
    return ProvidersOut(
        providers=[await _out(db, row, s, r.states) for row in rows],
        default_model_id=await _default_model_id(db),
        cloud_max_classification=registry.cloud_cap(s),
    )


@router.post("/providers", status_code=201)
async def create_provider(
    body: ProviderIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> ProviderOut:
    s = get_settings()
    slug = body.slug or _slugify(body.name)
    try:
        registry.validate_slug(slug)
        models = [registry.validate_model_name(m.strip()) for m in body.models if m.strip()]
    except registry.ProviderConfigError as exc:
        raise _422(str(exc)) from exc
    if await db.scalar(select(LlmProvider.id).where(LlmProvider.slug == slug)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A provider with slug {slug!r} already exists")
    self_hosted = body.self_hosted and body.kind == "api"
    locality = await _probe(body.base_url, body.kind, self_hosted)
    _check_clearance(body.max_classification, locality, s)
    # Default clearance: the most the locality allows for local providers; the most restrictive (public)
    # for anything that is not local, until the admin deliberately raises it.
    requested = body.max_classification
    if requested is None and locality != "local":
        requested = Classification.public
    row = LlmProvider(
        slug=slug,
        name=body.name,
        kind=body.kind,
        builtin=False,
        enabled=body.enabled,
        base_url=body.base_url.rstrip("/"),
        self_hosted=self_hosted,
        locality=locality,
        max_classification=requested,
        monthly_budget_usd=body.monthly_budget_usd,
        daily_request_limit=body.daily_request_limit,
        max_output_tokens=body.max_output_tokens,
        max_input_tokens=body.max_input_tokens,
        tokens_per_day=body.tokens_per_day,
        tokens_per_month=body.tokens_per_month,
        max_turn_tokens=body.max_turn_tokens,
        created_by=auth.user.id,
    )
    row.models = [LlmModel(model=m) for m in dict.fromkeys(models)]
    db.add(row)
    if body.api_key is not None:
        row.secret_name = registry.secret_name_for(slug)
        await vault.set_secret(
            db, row.secret_name, body.api_key, user_id=auth.user.id, description=f"API key of provider {body.name}"
        )
    await db.flush()
    audit(
        db,
        "admin.provider.create",
        user_id=auth.user.id,
        resource=f"provider:{slug}",
        details={
            "kind": body.kind,
            "base_url": row.base_url,
            "self_hosted": self_hosted,
            "locality": locality,
            "max_classification": requested.value if requested else None,
            "models": models,
            "limits": {n: getattr(row, n) for n in PROVIDER_LIMITS},
            "api_key_set": body.api_key is not None,
        },
    )
    await _changed(db)
    r = await load_router(db, s)
    return await _out(db, row, s, r.states)


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderPatch,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ProviderOut:
    s = get_settings()
    fields = body.model_fields_set
    if fields & SENSITIVE_PATCH and not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)
    row = await _row_or_404(db, provider_id)
    changes: dict[str, Any] = {}
    if row.builtin and fields & {"base_url", "self_hosted"}:
        env = "ERAG_OLLAMA_BASE_URL" if row.slug == registry.OLLAMA else "ERAG_OPENROUTER_BASE_URL"
        raise HTTPException(status.HTTP_409_CONFLICT, f"The URL of a built-in provider comes from {env}")
    if body.enabled is False and row.enabled and any(m.is_default for m in row.models):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This provider serves the default model; choose another default first"
        )

    locality = row.locality
    if "base_url" in fields or "self_hosted" in fields:
        new_url = (body.base_url or row.base_url).rstrip("/")
        new_self = (body.self_hosted if body.self_hosted is not None else row.self_hosted) and row.kind == "api"
        locality = await _probe(new_url, row.kind, new_self)
        if locality != "local" and row.locality == "local" and "max_classification" not in fields:
            # It just became a cloud endpoint: treat it like a new non-local provider.
            row.max_classification = Classification.public
            changes["max_classification"] = "public (locality changed to cloud)"
        row.base_url, row.self_hosted, row.locality = new_url, new_self, locality
        changes.update(base_url=new_url, self_hosted=new_self, locality=locality)
    if "max_classification" in fields:
        _check_clearance(body.max_classification, locality, s)
        row.max_classification = body.max_classification
        changes["max_classification"] = body.max_classification.value if body.max_classification else None
    limit_changes: dict[str, Any] = {}
    for name in PROVIDER_LIMITS:
        if name in fields and getattr(body, name) != getattr(row, name):
            limit_changes[name] = {"from": getattr(row, name), "to": getattr(body, name)}
            setattr(row, name, getattr(body, name))
    if limit_changes:
        # Limits may be raised as well as lowered (D2); every change is in the change history.
        audit(
            db,
            "admin.limits.provider",
            user_id=auth.user.id,
            resource=f"provider:{row.slug}",
            details={"changes": limit_changes, "env_defaults": _env_defaults(row, s)},
        )
    if body.name is not None:
        row.name = body.name
        changes["name"] = body.name
    if body.enabled is not None:
        row.enabled = body.enabled
        changes["enabled"] = body.enabled
    if body.api_key is not None:
        row.secret_name = row.secret_name or registry.secret_name_for(row.slug)
        await vault.set_secret(db, row.secret_name, body.api_key, user_id=auth.user.id)
    elif body.clear_api_key and row.secret_name and await vault.exists(db, row.secret_name):
        await vault.delete_secret(db, row.secret_name, user_id=auth.user.id)
    audit(
        db,
        "admin.provider.update",
        user_id=auth.user.id,
        resource=f"provider:{row.slug}",
        details={**changes, "api_key_changed": body.api_key is not None, "api_key_cleared": body.clear_api_key},
    )
    await db.flush()
    await _changed(db)
    r = await load_router(db, s)
    return await _out(db, await _row_or_404(db, provider_id, fresh=True), s, r.states)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    row = await _row_or_404(db, provider_id)
    if row.builtin:
        raise HTTPException(status.HTTP_409_CONFLICT, "Built-in providers cannot be deleted; disable them instead")
    if any(m.is_default for m in row.models):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This provider serves the default model; choose another default first"
        )
    if row.secret_name == registry.secret_name_for(row.slug) and await vault.exists(db, row.secret_name):
        await vault.delete_secret(db, row.secret_name, user_id=auth.user.id)
    audit(
        db,
        "admin.provider.delete",
        user_id=auth.user.id,
        resource=f"provider:{row.slug}",
        details={"name": row.name, "base_url": row.base_url, "models": [m.model for m in row.models]},
    )
    await db.delete(row)
    await _changed(db)


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Read-only request to the provider (model list), through the same egress guard as chat; no cost."""
    row = await _row_or_404(db, provider_id)
    key, _ = await registry.resolve_key(db, row)
    result = await registry.test_connection(row, key)
    audit(db, "admin.provider.test", user_id=auth.user.id, resource=f"provider:{row.slug}", details=result)
    await db.commit()
    return result


@router.post("/providers/{provider_id}/discover")
async def discover_provider_models(
    provider_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Ollama: GET /api/tags; OpenAI-compatible: GET /models. Models are listed, not added."""
    row = await _row_or_404(db, provider_id)
    key, _ = await registry.resolve_key(db, row)
    try:
        found = await registry.discover_models(row, key)
    except (registry.ProviderConfigError, BlockedDestination) as exc:
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Discovery failed: {exc}") from exc
    audit(
        db,
        "admin.provider.discover",
        user_id=auth.user.id,
        resource=f"provider:{row.slug}",
        details={"found": len(found)},
    )
    await db.commit()
    configured = {m.model for m in row.models}
    return {"models": [{"model": m, "configured": m in configured} for m in found]}


# --- Models -----------------------------------------------------------------------------------------


@router.post("/providers/{provider_id}/models", status_code=201)
async def add_model(
    provider_id: uuid.UUID,
    body: ModelIn,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModelOut:
    row = await _row_or_404(db, provider_id)
    try:
        model = registry.validate_model_name(body.model.strip())
    except ValueError as exc:
        raise _422(str(exc)) from exc
    if any(m.model == model for m in row.models):
        raise HTTPException(status.HTTP_409_CONFLICT, "Model already configured")
    m = LlmModel(provider_id=row.id, model=model)
    if registry.is_openrouter(row):
        # Best effort: take the price from OpenRouter's own model list so the worst-case check knows it.
        # Skipped without a key (the provider cannot be used then anyway; "Sync prices" fills it later).
        key, _ = await registry.resolve_key(db, row)
        price = None
        if key:
            try:
                price = (await registry.openrouter_prices(row, key)).get(model)
            except (registry.ProviderConfigError, BlockedDestination):
                price = None
        if price is not None:
            m.input_usd_per_mtok, m.output_usd_per_mtok = price
            m.price_source = "openrouter"
    db.add(m)
    audit(
        db,
        "admin.provider.model.add",
        user_id=auth.user.id,
        resource=f"provider:{row.slug}",
        details={"model": model, "price_usd_per_mtok": [m.input_usd_per_mtok, m.output_usd_per_mtok]},
    )
    await db.flush()
    out = await _model_out(db, row.slug, m)
    await _changed(db)
    return out


async def _model_or_404(db: AsyncSession, provider_id: uuid.UUID, model_id: uuid.UUID) -> LlmModel:
    m = await db.get(LlmModel, model_id)
    if m is None or m.provider_id != provider_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    return m


@router.patch("/providers/{provider_id}/models/{model_id}")
async def update_model(
    provider_id: uuid.UUID,
    model_id: uuid.UUID,
    body: ModelPatch,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModelOut:
    fields = body.model_fields_set
    sensitive = {*MODEL_LIMITS, *MODEL_PRICES, "max_classification", "jev_router", "supports_tools"}
    if fields & sensitive and not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)
    m = await _model_or_404(db, provider_id, model_id)
    slug = m.provider.slug
    s = get_settings()
    other: dict[str, Any] = {}
    if "max_classification" in fields and body.max_classification != m.max_classification:
        cap = registry.effective_clearance(m.provider.max_classification, m.provider.locality, s)
        if body.max_classification is not None and body.max_classification.rank > cap.rank:
            raise _422(f"A model can only lower its provider's clearance ({cap.value})")
        other["max_classification"] = {"from": m.max_classification, "to": body.max_classification}
        m.max_classification = body.max_classification
    if "supports_tools" in fields and body.supports_tools is not None and body.supports_tools != m.supports_tools:
        other["supports_tools"] = {"from": m.supports_tools, "to": body.supports_tools}
        m.supports_tools = body.supports_tools
    if "jev_router" in fields:
        if not (registry.is_openrouter(m.provider) and m.model == registry.JEV_ROUTER_MODEL):
            raise _422(f"Jev Router options only apply to openrouter:{registry.JEV_ROUTER_MODEL}")
        try:
            options = registry.validate_jev_router_options(body.jev_router or {})
        except registry.ProviderConfigError as exc:
            raise _422(str(exc)) from exc
        other["jev_router"] = {"from": (m.options or {}).get("jev_router"), "to": options}
        m.options = {**(m.options or {}), "jev_router": options}
    if other:
        audit(
            db,
            "admin.limits.model",
            user_id=auth.user.id,
            resource=f"model:{slug}:{m.model}",
            details={"changes": other},
        )
    if "enabled" in fields and body.enabled is not None and body.enabled != m.enabled:
        if not body.enabled and m.is_default:
            raise HTTPException(status.HTTP_409_CONFLICT, "This is the default model; choose another default first")
        m.enabled = body.enabled
        audit(
            db,
            "admin.provider.model.update",
            user_id=auth.user.id,
            resource=f"provider:{slug}",
            details={"model": m.model, "enabled": body.enabled},
        )
    limit_changes = {
        n: {"from": getattr(m, n), "to": getattr(body, n)}
        for n in MODEL_LIMITS
        if n in fields and getattr(body, n) != getattr(m, n)
    }
    price_changes = {
        n: {"from": getattr(m, n), "to": getattr(body, n)}
        for n in MODEL_PRICES
        if n in fields and getattr(body, n) != getattr(m, n)
    }
    for n, change in {**limit_changes, **price_changes}.items():
        setattr(m, n, change["to"])
    if limit_changes:
        audit(
            db,
            "admin.limits.model",
            user_id=auth.user.id,
            resource=f"model:{slug}:{m.model}",
            details={"changes": limit_changes},
        )
    if price_changes:
        m.price_source = "admin" if m.input_usd_per_mtok is not None or m.output_usd_per_mtok is not None else None
        audit(
            db,
            "admin.price.update",
            user_id=auth.user.id,
            resource=f"model:{slug}:{m.model}",
            details={"changes": price_changes, "source": m.price_source},
        )
    await db.flush()
    out = await _model_out(db, slug, m)
    await _changed(db)
    return out


@router.post("/providers/{provider_id}/prices/sync")
async def sync_prices(
    provider_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """OpenRouter: fill the prices of the configured models from its model list (`pricing`, USD per token).
    Other providers do not publish prices in a standard form: enter them by hand."""
    row = await _row_or_404(db, provider_id)
    if not registry.is_openrouter(row):
        raise _422("Only OpenRouter publishes prices in its model list; enter prices by hand for this provider")
    key, _ = await registry.resolve_key(db, row)
    try:
        prices = await registry.openrouter_prices(row, key)
    except (registry.ProviderConfigError, BlockedDestination) as exc:
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Price list failed: {exc}") from exc
    updated: dict[str, Any] = {}
    missing: list[str] = []
    for m in row.models:
        price = prices.get(m.model)
        if price is None:
            missing.append(m.model)
            continue
        if (m.input_usd_per_mtok, m.output_usd_per_mtok) != price:
            updated[m.model] = {"from": [m.input_usd_per_mtok, m.output_usd_per_mtok], "to": list(price)}
        m.input_usd_per_mtok, m.output_usd_per_mtok = price
        m.price_source = "openrouter"
    audit(
        db,
        "admin.price.sync",
        user_id=auth.user.id,
        resource=f"provider:{row.slug}",
        details={"changes": updated, "not_listed": missing},
    )
    await _changed(db)
    return {"updated": updated, "not_listed": missing}


@router.get("/providers/{provider_id}/usage/daily")
async def provider_daily_usage(
    provider_id: uuid.UUID,
    days: int = 30,
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Requests, tokens and cost per UTC day for the last `days` days (figures only)."""
    row = await _row_or_404(db, provider_id)
    return await usage.daily_series(db, provider=row.slug, days=max(1, min(days, 90)))


@router.get("/limits/history")
async def limits_history(
    limit: int = 100, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    """Every change of a limit, price or quota (from the audit log), newest first. A compromised admin session
    could raise budgets: this is where to look (ADR 0026)."""
    from app.models import AuditLog, User

    rows = (
        await db.execute(
            select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.user_id)
            .where(
                AuditLog.action.like("admin.limits.%")
                | AuditLog.action.like("admin.price.%")
                | AuditLog.action.like("admin.quota.%")
                | AuditLog.action.like("admin.router.%")
            )
            .order_by(AuditLog.ts.desc())
            .limit(max(1, min(limit, 500)))
        )
    ).all()
    return [
        {"ts": a.ts.isoformat(), "who": email, "action": a.action, "resource": a.resource, "details": a.details}
        for a, email in rows
    ]


@router.delete("/providers/{provider_id}/models/{model_id}", status_code=204)
async def delete_model(
    provider_id: uuid.UUID,
    model_id: uuid.UUID,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    m = await _model_or_404(db, provider_id, model_id)
    if m.is_default:
        raise HTTPException(status.HTTP_409_CONFLICT, "This is the default model; choose another default first")
    if m.env_managed:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This model comes from the environment (ERAG_*_MODEL); disable it instead"
        )
    audit(
        db,
        "admin.provider.model.delete",
        user_id=auth.user.id,
        resource=f"provider:{m.provider.slug}",
        details={"model": m.model},
    )
    await db.delete(m)
    await _changed(db)


@router.put("/models/default")
async def set_default_model(
    body: DefaultIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> dict[str, str | None]:
    """The model the chat uses when the user picks none. Routing still enforces clearance: if the default
    is not cleared for a request's classification, the router falls back to a cleared (local) model."""
    previous = await _default_model_id(db)
    target: LlmModel | None = None
    if body.model_id:
        slug, sep, model = body.model_id.partition(":")
        if not sep:
            raise _422("model_id must look like <provider>:<model>")
        target = await db.scalar(
            select(LlmModel).join(LlmProvider).where(LlmProvider.slug == slug, LlmModel.model == model)
        )
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown model")
        if not target.enabled or not target.provider.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Enable the model and its provider before making it the default"
            )
    await db.execute(update(LlmModel).where(LlmModel.is_default).values(is_default=False))
    await db.flush()
    if target is not None:
        target.is_default = True
    audit(
        db,
        "admin.model.default",
        user_id=auth.user.id,
        resource=f"model:{body.model_id or 'automatic'}",
        details={"previous": previous, "new": body.model_id},
    )
    await _changed(db)
    return {"default_model_id": body.model_id}

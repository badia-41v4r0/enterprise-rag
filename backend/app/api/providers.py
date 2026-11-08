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
from app.llm.router import invalidate_router_cache, load_router, validate_openrouter_model
from app.models import Classification, LlmModel, LlmProvider
from app.services import vault
from app.services.audit import audit
from app.services.spend import month_spend, requests_today

router = APIRouter(prefix="/api/admin", tags=["providers"])

Kind = Literal["ollama", "api"]


class ModelOut(BaseModel):
    id: uuid.UUID
    model_id: str
    model: str
    enabled: bool
    env_managed: bool
    is_default: bool


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
    free_only: bool
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


class ModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ModelPatch(BaseModel):
    enabled: bool


class DefaultIn(BaseModel):
    model_id: str | None = Field(default=None, max_length=250)  # "<slug>:<model>"; null = automatic


SENSITIVE_PATCH = {"base_url", "self_hosted", "api_key", "clear_api_key", "max_classification"}


def _422(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "provider"


async def _row_or_404(db: AsyncSession, provider_id: uuid.UUID, *, fresh: bool = False) -> LlmProvider:
    row = await db.get(LlmProvider, provider_id, populate_existing=fresh)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    return row


def _check_limits(row: LlmProvider, s: Settings, budget: float | None, daily: int | None) -> None:
    """The admin panel may only lower the environment ceilings (ADR 0016, ADR 0024)."""
    cap = registry.budget_cap(row, s)
    if budget is not None and budget > cap:
        env = (
            "ERAG_OPENROUTER_MONTHLY_BUDGET_USD" if registry.is_openrouter(row) else "ERAG_PROVIDER_MONTHLY_BUDGET_USD"
        )
        raise _422(f"The monthly ceiling cannot exceed {env} ({cap})")
    env_daily = registry.daily_cap_env(row, s)
    if daily is not None and env_daily is not None and daily > env_daily:
        raise _422(f"The daily request limit cannot exceed ERAG_PROVIDER_DAILY_REQUEST_CAP ({env_daily})")


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
        free_only=registry.is_openrouter(row) and not s.openrouter_allow_paid,
        available=bool(state and state.available),
        status_reason=(state.reason if state else "") or ("" if state and state.available else "not loaded"),
        models=[
            ModelOut(
                id=m.id,
                model_id=f"{row.slug}:{m.model}",
                model=m.model,
                enabled=m.enabled,
                env_managed=m.env_managed,
                is_default=m.is_default,
            )
            for m in row.models
        ],
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
        created_by=auth.user.id,
    )
    _check_limits(row, s, body.monthly_budget_usd, body.daily_request_limit)
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
    if "monthly_budget_usd" in fields or "daily_request_limit" in fields:
        budget = body.monthly_budget_usd if "monthly_budget_usd" in fields else row.monthly_budget_usd
        daily = body.daily_request_limit if "daily_request_limit" in fields else row.daily_request_limit
        _check_limits(row, s, budget, daily)
        row.monthly_budget_usd, row.daily_request_limit = budget, daily
        changes.update(monthly_budget_usd=budget, daily_request_limit=daily)
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
    s = get_settings()
    try:
        model = registry.validate_model_name(body.model.strip())
        if registry.is_openrouter(row):
            validate_openrouter_model(model, s.openrouter_allow_paid)
    except ValueError as exc:
        raise _422(str(exc)) from exc
    if any(m.model == model for m in row.models):
        raise HTTPException(status.HTTP_409_CONFLICT, "Model already configured")
    m = LlmModel(provider_id=row.id, model=model)
    db.add(m)
    audit(
        db, "admin.provider.model.add", user_id=auth.user.id, resource=f"provider:{row.slug}", details={"model": model}
    )
    await db.flush()
    out = ModelOut(
        id=m.id, model_id=f"{row.slug}:{model}", model=model, enabled=True, env_managed=False, is_default=False
    )
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
    m = await _model_or_404(db, provider_id, model_id)
    if not body.enabled and m.is_default:
        raise HTTPException(status.HTTP_409_CONFLICT, "This is the default model; choose another default first")
    m.enabled = body.enabled
    slug = m.provider.slug
    audit(
        db,
        "admin.provider.model.update",
        user_id=auth.user.id,
        resource=f"provider:{slug}",
        details={"model": m.model, "enabled": body.enabled},
    )
    out = ModelOut(
        id=m.id,
        model_id=f"{slug}:{m.model}",
        model=m.model,
        enabled=m.enabled,
        env_managed=m.env_managed,
        is_default=m.is_default,
    )
    await _changed(db)
    return out


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

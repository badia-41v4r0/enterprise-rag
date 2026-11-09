"""Credential store and provider settings (admin). Values are write-only; every change needs step-up."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_admin, require_step_up
from app.core.config import get_settings
from app.core.crypto import CryptoError
from app.core.db import get_db
from app.llm import registry
from app.llm.router import invalidate_router_cache, resolve_openrouter
from app.models import LlmProvider, Secret
from app.services import vault
from app.services.audit import audit
from app.services.settings_store import OPENROUTER_KEY, get_setting, put_setting
from app.services.spend import month_spend

router = APIRouter(prefix="/api/admin", tags=["secrets"])


class SecretOut(BaseModel):
    name: str
    description: str
    version: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    last_used_by: str | None
    used_by: list[str]


class SecretIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=16_000)
    description: str | None = Field(default=None, max_length=500)


class OpenRouterOut(BaseModel):
    configured: bool
    key_source: str
    enabled: bool
    model: str
    monthly_budget_usd: float
    budget_cap_usd: float
    spent_this_month_usd: float
    cloud_max_classification: str


class OpenRouterIn(BaseModel):
    enabled: bool | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    # Write-only: stored in the vault as `openrouter_api_key`, never returned.
    api_key: str | None = Field(default=None, min_length=1, max_length=500)


@router.get("/secrets")
async def list_secrets(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[SecretOut]:
    refs = await vault.references(db)
    rows = (await db.scalars(select(Secret).order_by(Secret.name))).all()
    return [
        SecretOut(
            name=r.name,
            description=r.description,
            version=r.version,
            created_at=r.created_at,
            updated_at=r.updated_at,
            last_used_at=r.last_used_at,
            last_used_by=r.last_used_by,
            used_by=refs.get(r.name, []),
        )
        for r in rows
    ]


@router.put("/secrets", status_code=204)
async def put_secret(
    body: SecretIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    """Create or replace a secret. The value is never returned by any endpoint."""
    try:
        await vault.set_secret(db, body.name, body.value, user_id=auth.user.id, description=body.description)
    except (vault.VaultError, CryptoError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await db.commit()
    invalidate_router_cache()


@router.delete("/secrets/{name}", status_code=204)
async def remove_secret(
    name: str, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    try:
        await vault.delete_secret(db, name, user_id=auth.user.id)
    except vault.SecretNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Secret not found") from exc
    await db.commit()
    invalidate_router_cache()


async def _openrouter_out(db: AsyncSession) -> OpenRouterOut:
    s = get_settings()
    conf = await resolve_openrouter(db, s, reveal=False)
    return OpenRouterOut(
        configured=bool(conf.api_key),
        key_source=conf.key_source,
        enabled=conf.enabled,
        model=conf.model,
        monthly_budget_usd=conf.budget_usd,
        budget_cap_usd=s.openrouter_monthly_budget_usd,
        spent_this_month_usd=await month_spend(db, "openrouter"),
        cloud_max_classification=s.cloud_max_classification,
    )


@router.get("/providers/openrouter")
async def get_openrouter(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> OpenRouterOut:
    return await _openrouter_out(db)


@router.put("/providers/openrouter")
async def put_openrouter(
    body: OpenRouterIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> OpenRouterOut:
    """Change the OpenRouter key, model, ceiling or on/off without a restart (providers are rebuilt)."""
    s = get_settings()
    # The provider registry row is the source of truth (ADR 0023); this endpoint is kept for compatibility.
    await registry.ensure_builtin_providers(db, s)
    row = await db.scalar(select(LlmProvider).where(LlmProvider.builtin, LlmProvider.slug == registry.OPENROUTER))
    assert row is not None
    if body.enabled is False and any(m.is_default for m in row.models):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An OpenRouter model is the default model; choose another default first"
        )
    conf = await get_setting(db, OPENROUTER_KEY)
    if body.model is not None:
        try:
            registry.validate_model_name(body.model)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        conf["model"] = body.model
    if body.monthly_budget_usd is not None:
        # The environment value is the initial default; the admin may lower or raise it (ADR 0026).
        if body.monthly_budget_usd != row.monthly_budget_usd:
            audit(
                db,
                "admin.limits.provider",
                user_id=auth.user.id,
                resource="provider:openrouter",
                details={
                    "changes": {"monthly_budget_usd": {"from": row.monthly_budget_usd, "to": body.monthly_budget_usd}}
                },
            )
        conf["monthly_budget_usd"] = body.monthly_budget_usd
    if body.enabled is not None:
        conf["enabled"] = body.enabled
    await put_setting(db, OPENROUTER_KEY, conf, auth.user.id)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.monthly_budget_usd is not None:
        row.monthly_budget_usd = body.monthly_budget_usd
    if body.api_key is not None:
        await vault.set_secret(db, vault.OPENROUTER_SECRET, body.api_key, user_id=auth.user.id)
    audit(
        db,
        "admin.provider.update",
        user_id=auth.user.id,
        resource="provider:openrouter",
        details={
            **body.model_dump(exclude={"api_key"}, exclude_none=True),
            "api_key_changed": body.api_key is not None,
        },
    )
    await db.commit()
    await registry.ensure_builtin_providers(db, s)  # a new model replaces the env-managed one
    invalidate_router_cache()
    return await _openrouter_out(db)

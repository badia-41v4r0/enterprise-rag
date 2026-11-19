"""Classification-based model router (ADR 0003). Runs after retrieval and after each tool result."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm.providers.base import LLMProvider
from app.llm.providers.claude_code import ClaudeCodeProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.models import Classification
from app.services.usage import ModelLimits


class RoutingRefused(Exception):
    def __init__(self, classification: Classification, reason: str) -> None:
        super().__init__(reason)
        self.classification = classification
        self.reason = reason


@dataclass
class ModelInfo:
    id: str
    provider: str
    model: str
    locality: str
    max_classification: Classification
    is_default: bool = False


class ModelRouter:
    def __init__(
        self,
        providers: list[LLMProvider],
        default_id: str | None = None,
        budgets: dict[str, float] | None = None,
        daily_caps: dict[str, int | None] | None = None,
        limits: dict[str, ModelLimits] | None = None,
    ) -> None:
        self.providers = providers
        # The admin's default (a provider:model id); used only when it is cleared for the request.
        self.default_id = default_id
        # Monthly spend ceiling (USD) and daily request cap per metered provider; see ADR 0016 and 0024.
        self.budgets = budgets or {}
        self.daily_caps = daily_caps or {}
        # Per provider slug: evaluated locality, clearance and availability (registry.ProviderState).
        self.states: dict[str, Any] = {}
        # Per model id: effective limits and price (ADR 0026). Missing entries get defaults (limits_for).
        self.limits: dict[str, ModelLimits] = dict(limits or {})

    def models(self) -> list[ModelInfo]:
        return [
            ModelInfo(p.model_id, p.provider, p.model, p.locality, p.max_classification, p.model_id == self.default_id)
            for p in self.providers
        ]

    def permitted(self, classification: Classification) -> list[LLMProvider]:
        return [p for p in self.providers if classification.rank <= p.max_classification.rank]

    def choose(self, classification: Classification, requested: str | None = None) -> LLMProvider:
        """Requested model if cleared, else the default if cleared, else a cleared model (local first).

        Clearance always wins: neither the user's choice nor the admin's default can send data to a
        model that is not cleared for it (ADR 0003, ADR 0023)."""
        allowed = self.permitted(classification)
        if not allowed:
            raise RoutingRefused(
                classification,
                f"No model is cleared for {classification.value} data on this installation, so the request "
                "was not sent to any model.",
            )
        if requested:
            for p in allowed:
                if p.model_id == requested:
                    return p
        if self.default_id:
            for p in allowed:
                if p.model_id == self.default_id:
                    return p
        # Prefer local models: free, and the only option for confidential data.
        return sorted(allowed, key=lambda p: 0 if p.locality == "local" else 1)[0]

    def get(self, model_id: str) -> LLMProvider | None:
        return next((p for p in self.providers if p.model_id == model_id), None)

    def is_metered(self, provider: LLMProvider) -> bool:
        return provider.provider in self.budgets or provider.locality == "cloud"

    def limits_for(self, provider: LLMProvider) -> ModelLimits:
        """Limits of a model built from the registry, or defaults for one built by hand (dev, tests)."""
        lim = self.limits.get(provider.model_id)
        if lim is not None:
            return lim
        s = get_settings()
        metered = self.is_metered(provider)
        return ModelLimits(
            provider=provider.provider,
            model=provider.model,
            metered=metered,
            max_output_tokens=s.provider_max_output_tokens,
            provider_monthly_usd=self.budgets.get(provider.provider, s.openrouter_monthly_budget_usd)
            if metered
            else None,
            daily_requests=self.daily_caps.get(provider.provider),
            price_known=not metered,
        )


@dataclass(frozen=True)
class OpenRouterConfig:
    """Resolved OpenRouter settings: key from the vault first, then the env var (bootstrap fallback)."""

    api_key: str | None
    model: str
    budget_usd: float
    enabled: bool = True
    key_source: str = "none"  # "vault" | "env" | "none"


def env_openrouter(s: Settings) -> OpenRouterConfig:
    key = s.openrouter_api_key.get_secret_value() if s.openrouter_api_key is not None else ""
    return OpenRouterConfig(
        api_key=key or None,
        model=s.openrouter_model,
        budget_usd=s.openrouter_monthly_budget_usd,
        key_source="env" if key else "none",
    )


def build_providers(settings: Settings | None = None, openrouter: OpenRouterConfig | None = None) -> list[LLMProvider]:
    s = settings or get_settings()
    orc = openrouter or env_openrouter(s)
    providers: list[LLMProvider] = [
        OpenAICompatProvider(
            provider="ollama",
            base_url=f"{s.ollama_base_url.rstrip('/')}/v1",
            model=s.local_chat_model,
            locality="local",
            max_classification=Classification.confidential,
            strip_think=s.local_chat_strip_think,
        )
    ]
    if orc.enabled and orc.api_key:
        providers.append(
            OpenAICompatProvider(
                provider="openrouter",
                base_url=s.openrouter_base_url,
                model=orc.model,
                locality="cloud",
                max_classification=Classification(s.cloud_max_classification),
                api_key=orc.api_key,
                extra_headers={"HTTP-Referer": "https://badiapartners.com", "X-Title": "EnterpriseRAG"},
                # Ask OpenRouter to report the real cost of each request in the final usage chunk.
                extra_body={"usage": {"include": True}},
            )
        )
    if s.claude_code_enabled and not s.is_prod:
        providers.append(ClaudeCodeProvider(binary=s.claude_code_bin))
    return providers


def get_router() -> ModelRouter:
    """Router from environment settings only (used by tests and tools without a database)."""
    s = get_settings()
    return ModelRouter(build_providers(s), budgets={"openrouter": s.openrouter_monthly_budget_usd})


# --- Runtime configuration (Providers screen, ADR 0018 and 0023) ----------------------------------

_cache: dict[str, tuple[tuple[object, ...], ModelRouter]] = {}
log = logging.getLogger(__name__)


async def _fingerprint(db: AsyncSession, s: Settings) -> tuple[object, ...]:
    """Cheap summary of everything the router depends on; any change rebuilds it (no restart)."""
    from app.models import AppSetting, LlmModel, LlmProvider, Secret
    from app.services.settings_store import OPENROUTER_KEY

    prov = (await db.execute(select(func.count(LlmProvider.id), func.max(LlmProvider.updated_at)))).one()
    mods = (await db.execute(select(func.count(LlmModel.id), func.max(LlmModel.updated_at)))).one()
    secs = (
        await db.execute(select(func.count(Secret.id), func.max(Secret.updated_at), func.sum(Secret.version)))
    ).one()
    setting = await db.scalar(select(AppSetting.updated_at).where(AppSetting.key == OPENROUTER_KEY))
    org = await db.scalar(select(AppSetting.updated_at).where(AppSetting.key == "organization"))
    return (id(s), tuple(prov), tuple(mods), tuple(secs), setting, org)


async def resolve_openrouter(
    db: AsyncSession, settings: Settings | None = None, *, reveal: bool = True
) -> OpenRouterConfig:
    """The built-in OpenRouter row: vault key first, then ERAG_OPENROUTER_API_KEY.

    The environment ceiling is the default; the admin's value wins (ADR 0026)."""
    from app.llm import registry
    from app.models import LlmProvider

    s = settings or get_settings()
    await registry.ensure_builtin_providers(db, s)
    row = await db.scalar(
        select(LlmProvider)
        .where(LlmProvider.builtin, LlmProvider.slug == registry.OPENROUTER)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    key, source = await registry.resolve_key(db, row, s, reveal=reveal)
    model = next((m.model for m in row.models if m.env_managed), s.openrouter_model)
    return OpenRouterConfig(
        api_key=key,
        model=model,
        budget_usd=registry.effective_budget(row, s),
        enabled=row.enabled,
        key_source=source,
    )


async def load_router(db: AsyncSession, settings: Settings | None = None) -> ModelRouter:
    """Router built from the provider registry; rebuilt only when providers, models, the default or a secret
    changed. Built-in providers are (re)created from the environment first, so nothing needs an admin."""
    from app.llm import registry

    s = settings or get_settings()
    await registry.refresh_cloud_cap(db)  # the administrator's cloud ceiling (Setup > Organization)
    cached = _cache.get("router")
    if cached is not None and cached[0] == await _fingerprint(db, s):
        return cached[1]
    await registry.ensure_builtin_providers(db, s)
    built = await registry.build_from_db(db, s)
    providers = list(built.providers)
    if s.claude_code_enabled and not s.is_prod:
        # Dev-only, env-gated, never stored in the database.
        providers.append(ClaudeCodeProvider(binary=s.claude_code_bin))
    router = ModelRouter(
        providers, default_id=built.default_id, budgets=built.budgets, daily_caps=built.daily_caps, limits=built.limits
    )
    router.states = dict(built.states)
    _cache["router"] = (await _fingerprint(db, s), router)
    return router


def invalidate_router_cache() -> None:
    _cache.clear()

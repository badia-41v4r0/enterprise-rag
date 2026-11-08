"""Classification-based model router (ADR 0003). Runs after retrieval and after each tool result."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm.providers.base import LLMProvider
from app.llm.providers.claude_code import ClaudeCodeProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.models import Classification


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


class ModelRouter:
    def __init__(
        self, providers: list[LLMProvider], default_id: str | None = None, budgets: dict[str, float] | None = None
    ) -> None:
        self.providers = providers
        self.default_id = default_id
        # Monthly spend ceiling per cloud provider (USD); see ADR 0016.
        self.budgets = budgets or {}

    def models(self) -> list[ModelInfo]:
        return [ModelInfo(p.model_id, p.provider, p.model, p.locality, p.max_classification) for p in self.providers]

    def permitted(self, classification: Classification) -> list[LLMProvider]:
        return [p for p in self.providers if classification.rank <= p.max_classification.rank]

    def choose(self, classification: Classification, requested: str | None = None) -> LLMProvider:
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


def validate_openrouter_model(model: str, allow_paid: bool) -> None:
    """Fail loudly at startup when a paid OpenRouter model is configured but paid models are not allowed."""
    if not allow_paid and not model.endswith(":free"):
        raise ValueError(
            f"ERAG_OPENROUTER_MODEL={model!r} is not a free model (id must end in ':free'). "
            "Set ERAG_OPENROUTER_ALLOW_PAID=true to allow paid models (spend is capped by "
            "ERAG_OPENROUTER_MONTHLY_BUDGET_USD)."
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
        validate_openrouter_model(orc.model, s.openrouter_allow_paid)
        providers.append(
            OpenAICompatProvider(
                provider="openrouter",
                base_url=s.openrouter_base_url,
                model=orc.model,
                locality="cloud",
                max_classification=Classification(s.cloud_max_classification),
                api_key=orc.api_key,
                extra_headers={"HTTP-Referer": "https://badiapartners.com", "X-Title": "EnterpriseRAG"},
                free_only=not s.openrouter_allow_paid,
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


# --- Runtime configuration (Providers screen, ADR 0018) -----------------------------------------

_cache: dict[str, tuple[tuple[object, ...], ModelRouter, OpenRouterConfig]] = {}
log = logging.getLogger(__name__)


async def _fingerprint(db: AsyncSession) -> tuple[object, ...]:
    from app.models import AppSetting, Secret
    from app.services.settings_store import OPENROUTER_KEY
    from app.services.vault import OPENROUTER_SECRET

    setting = await db.scalar(select(AppSetting.updated_at).where(AppSetting.key == OPENROUTER_KEY))
    secret = (
        await db.execute(
            select(Secret.version, Secret.updated_at, Secret.key_id).where(Secret.name == OPENROUTER_SECRET)
        )
    ).first()
    return (setting, tuple(secret) if secret else None)


async def resolve_openrouter(
    db: AsyncSession, settings: Settings | None = None, *, reveal: bool = True
) -> OpenRouterConfig:
    """Vault first, then ERAG_OPENROUTER_API_KEY. The UI ceiling can only lower the env ceiling."""
    from app.services import vault
    from app.services.settings_store import OPENROUTER_KEY, get_setting

    s = settings or get_settings()
    env = env_openrouter(s)
    conf = await get_setting(db, OPENROUTER_KEY)
    key, source = env.api_key, env.key_source
    if await vault.exists(db, vault.OPENROUTER_SECRET):
        # reveal=False: status only (no decryption, no audit noise); the key is replaced by a marker.
        key = await vault.reveal(db, vault.OPENROUTER_SECRET, consumer="provider:openrouter") if reveal else "<vault>"
        source = "vault"
    budget = min(float(conf.get("monthly_budget_usd", env.budget_usd)), s.openrouter_monthly_budget_usd)
    return OpenRouterConfig(
        api_key=key,
        model=str(conf.get("model") or env.model),
        budget_usd=max(0.0, budget),
        enabled=bool(conf.get("enabled", True)),
        key_source=source,
    )


async def load_router(db: AsyncSession, settings: Settings | None = None) -> ModelRouter:
    """Providers rebuilt only when the OpenRouter setting or its secret changed (no restart needed)."""
    s = settings or get_settings()
    fp = await _fingerprint(db)
    cached = _cache.get("router")
    if cached is not None and cached[0] == fp:
        return cached[1]
    orc = await resolve_openrouter(db, s)
    await db.commit()  # persist the audited secret use
    try:
        providers = build_providers(s, orc)
    except ValueError as exc:
        log.error("OpenRouter disabled: %s", exc)
        providers = build_providers(s, OpenRouterConfig(None, orc.model, orc.budget_usd, False, orc.key_source))
    router = ModelRouter(providers, budgets={"openrouter": orc.budget_usd})
    _cache["router"] = (fp, router, orc)
    return router


def invalidate_router_cache() -> None:
    _cache.clear()

"""Classification-based model router (ADR 0003). Runs after retrieval and after each tool result."""

from __future__ import annotations

from dataclasses import dataclass

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
    def __init__(self, providers: list[LLMProvider], default_id: str | None = None) -> None:
        self.providers = providers
        self.default_id = default_id

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


def build_providers(settings: Settings | None = None) -> list[LLMProvider]:
    s = settings or get_settings()
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
    if s.openrouter_api_key is not None and s.openrouter_api_key.get_secret_value():
        validate_openrouter_model(s.openrouter_model, s.openrouter_allow_paid)
        providers.append(
            OpenAICompatProvider(
                provider="openrouter",
                base_url=s.openrouter_base_url,
                model=s.openrouter_model,
                locality="cloud",
                max_classification=Classification(s.cloud_max_classification),
                api_key=s.openrouter_api_key.get_secret_value(),
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
    return ModelRouter(build_providers())

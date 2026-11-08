"""Provider interface and the egress hook every outgoing LLM request passes through."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.models import Classification

Locality = Literal["local", "cloud"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as emitted by the model

    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


@dataclass
class TextDelta:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolCallsEvent:
    calls: list[ToolCall]
    type: Literal["tool_calls"] = "tool_calls"


@dataclass
class Done:
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    type: Literal["done"] = "done"


StreamEvent = TextDelta | ToolCallsEvent | Done


class ProviderError(RuntimeError):
    pass


class ClassificationViolation(ProviderError):
    """Raised by a provider asked to process data above its clearance (second line of defense)."""


class LLMProvider(Protocol):
    provider: str  # "ollama" | "openrouter" | "claude-code"
    model: str
    locality: Locality
    max_classification: Classification

    @property
    def model_id(self) -> str: ...

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        classification: Classification,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


# --- Egress -----------------------------------------------------------------------------------


@dataclass
class EgressRecord:
    provider: str
    model: str
    locality: Locality
    url: str
    classification: Classification
    payload: dict[str, Any]


class EgressBlocked(ProviderError):
    pass


EgressHook = Callable[[EgressRecord], Awaitable[None]]
_egress_hooks: list[EgressHook] = []


def add_egress_hook(hook: EgressHook) -> None:
    _egress_hooks.append(hook)


def remove_egress_hook(hook: EgressHook) -> None:
    if hook in _egress_hooks:
        _egress_hooks.remove(hook)


async def emit_egress(record: EgressRecord) -> None:
    """Called by providers right before a request leaves the process. A hook may raise EgressBlocked."""
    for hook in list(_egress_hooks):
        await hook(record)


def check_clearance(provider_max: Classification, classification: Classification, model_id: str) -> None:
    if classification.rank > provider_max.rank:
        raise ClassificationViolation(
            f"{model_id} is cleared for {provider_max.value} data, request is {classification.value}"
        )

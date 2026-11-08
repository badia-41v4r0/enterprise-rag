"""One OpenAI-compatible streaming client used for Ollama (/v1) and OpenRouter (ADR 0011)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.providers.base import (
    Done,
    EgressRecord,
    Locality,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
    ToolSpec,
    check_clearance,
    emit_egress,
)
from app.models import Classification

THINK_END = "</think>"


class ThinkStripper:
    """Drop a model's inline reasoning (everything up to `</think>`) from the visible stream.

    Text is held back until `</think>` is seen; if the stream ends without it, the held text
    is released as the answer.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.done = not enabled
        self.buf = ""

    def feed(self, text: str) -> str:
        if self.done:
            return text
        self.buf += text
        idx = self.buf.find(THINK_END)
        if idx < 0:
            return ""
        self.done = True
        out = self.buf[idx + len(THINK_END) :].lstrip()
        self.buf = ""
        return out

    def flush(self) -> str:
        if self.done:
            return ""
        self.done = True
        out = self.buf.replace("<think>", "").strip()
        self.buf = ""
        return out


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        locality: Locality,
        max_classification: Classification,
        api_key: str | None = None,
        strip_think: bool = False,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.locality: Locality = locality
        # A cloud provider can never be cleared for confidential data, whatever the config says.
        if locality == "cloud" and max_classification == Classification.confidential:
            max_classification = Classification.internal
        self.max_classification = max_classification
        self.api_key = api_key
        self.strip_think = strip_think
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        self.transport = transport

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

    def build_payload(self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True, "temperature": 0.2}
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        classification: Classification,
    ) -> AsyncIterator[StreamEvent]:
        check_clearance(self.max_classification, classification, self.model_id)
        payload = self.build_payload(messages, tools)
        url = f"{self.base_url}/chat/completions"
        await emit_egress(EgressRecord(self.provider, self.model, self.locality, url, classification, payload))

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        think = ThinkStripper(self.strip_think)
        calls: dict[int, dict[str, str]] = {}
        finish = "stop"
        usage: dict[str, Any] = {}
        async with (
            httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client,
            client.stream("POST", url, json=payload, headers=headers) as resp,
        ):
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")[:500]
                raise ProviderError(f"{self.model_id} returned HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise ProviderError(f"{self.model_id} stream error: {chunk['error']}")
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        visible = think.feed(delta["content"])
                        if visible:
                            yield TextDelta(visible)
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(int(tc.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        rest = think.flush()
        if rest:
            yield TextDelta(rest)
        if calls:
            yield ToolCallsEvent(
                [
                    ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"])
                    for i, c in sorted(calls.items())
                ]
            )
            finish = "tool_calls"
        yield Done(finish_reason=finish, usage=usage)

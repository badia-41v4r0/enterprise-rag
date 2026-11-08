"""Real provider tests (opt-in markers, skipped when the provider is unavailable).

Assertions target structure and authorization boundaries, never exact wording.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import get_settings
from app.llm.providers.base import Done, TextDelta, ToolCallsEvent, ToolSpec
from app.llm.providers.claude_code import ClaudeCodeProvider
from app.llm.router import build_providers
from app.main import create_app
from app.models import Classification
from tests.security.conftest import DEMO_PASSWORD

INVOICES = ToolSpec(
    name="list_invoices",
    description="List invoices, optionally filtered by status and minimum amount in EUR.",
    parameters={
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["open", "paid"]}, "min_amount": {"type": "number"}},
    },
)


def ollama():
    return next(p for p in build_providers() if p.provider == "ollama")


@pytest.mark.provider_ollama
async def test_ollama_streams_text_without_reasoning() -> None:
    events = [
        e
        async for e in ollama().stream_chat(
            [{"role": "user", "content": "Reply with one short sentence about the sea."}],
            classification=Classification.public,
        )
    ]
    assert isinstance(events[-1], Done)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text.strip() and "</think>" not in text


@pytest.mark.provider_ollama
async def test_ollama_tool_call_structure() -> None:
    events = [
        e
        async for e in ollama().stream_chat(
            [{"role": "user", "content": "Show me open invoices over 10000 EUR."}],
            tools=[INVOICES],
            classification=Classification.confidential,
        )
    ]
    calls = [c for e in events if isinstance(e, ToolCallsEvent) for c in e.calls]
    assert calls, "the local model is expected to call the tool for this request"
    assert calls[0].name == "list_invoices"
    assert isinstance(json.loads(calls[0].arguments), dict)


async def _chat(email: str, question: str) -> list[dict]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=600) as c:
        r = await c.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        r = await c.post("/api/chat", json={"message": question})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]


@pytest.mark.db
@pytest.mark.embeddings
@pytest.mark.provider_ollama
async def test_signature_scenario_end_to_end(acme: None) -> None:
    q = "What is the salary band for a senior engineer?"
    hr = await _chat("hr@acme.test", q)
    route = next(e for e in hr if e["type"] == "route")
    assert route["model"].startswith("ollama:") and route["classification"] == "confidential"
    done = hr[-1]
    assert done["type"] == "done" and done["classification"] == "confidential" and done["answer"]
    assert "</think>" not in done["answer"]
    assert any(c["title"].startswith("salary bands") for c in next(e for e in hr if e["type"] == "citations")["items"])

    eng = await _chat("engineer@acme.test", q)
    cites = next(e for e in eng if e["type"] == "citations")["items"]
    assert all(c["container"] != "HR Confidential" for c in cites)
    answer = eng[-1]["answer"]
    for secret in ("CANARY-HR-9XK3", "58,000", "72,000"):
        assert secret not in answer


@pytest.mark.provider_openrouter
async def test_openrouter_internal_request() -> None:
    p = next(p for p in build_providers() if p.provider == "openrouter")
    events = [
        e
        async for e in p.stream_chat([{"role": "user", "content": "Say hello."}], classification=Classification.public)
    ]
    assert isinstance(events[-1], Done)


@pytest.mark.provider_claude_code
async def test_claude_code_provider() -> None:
    p = ClaudeCodeProvider(binary=get_settings().claude_code_bin)
    events = [
        e
        async for e in p.stream_chat(
            [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say hello in one word."}],
            classification=Classification.public,
        )
    ]
    assert isinstance(events[-1], Done)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip()

"""Providers added from the admin API, exercised against the REAL local Ollama (ADR 0023, 0024).

- kind "ollama": a second registration of the host Ollama (`GET /api/tags` discovery);
- kind "api": Ollama's own OpenAI-compatible `/v1` used as a real generic API server (`GET /models`),
  declared self-hosted because it lives on loopback.
No fake server: every answer below comes from the real local model.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.llm.router import invalidate_router_cache
from app.main import create_app
from app.models import AuditLog, ProviderSpend, User
from app.services.spend import month_key, requests_today
from tests.security.conftest import DEMO_PASSWORD

QWEN = "qwen3:4b-instruct-2507-q4_K_M"
PUBLIC_Q = "What is new in version 4.2 of the ACME Cloud Planner mobile app?"

pytestmark = [pytest.mark.db, pytest.mark.embeddings, pytest.mark.provider_ollama]


async def _client(email: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t", timeout=600)
    r = await c.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


async def _chat(c: httpx.AsyncClient, question: str, model: str | None = None) -> list[dict]:
    r = await c.post("/api/chat", json={"message": question, "model": model})
    assert r.status_code == 200
    return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]


async def test_add_ollama_and_api_providers_discover_set_default_and_answer(acme: None) -> None:
    invalidate_router_cache()
    admin = await _client("admin@acme.test")
    hire = await _client("newhire@acme.test")
    created: list[str] = []
    try:
        # Kind "ollama": the host instance again, under another name, with no model yet.
        r = await admin.post(
            "/api/admin/providers", json={"name": "LAN Ollama", "kind": "ollama", "base_url": "http://127.0.0.1:11434"}
        )
        assert r.status_code == 201, r.text
        lan = r.json()
        created.append(lan["id"])
        assert lan["locality"] == "local" and lan["max_classification"] == "confidential" and lan["models"] == []
        test = (await admin.post(f"/api/admin/providers/{lan['id']}/test")).json()
        assert test["ok"] and test["locality"] == "local" and test["models_available"] >= 1
        found = (await admin.post(f"/api/admin/providers/{lan['id']}/discover")).json()["models"]
        assert QWEN in [m["model"] for m in found]
        r = await admin.post(f"/api/admin/providers/{lan['id']}/models", json={"model": QWEN})
        assert r.status_code == 201 and r.json()["model_id"] == f"lan-ollama:{QWEN}"

        # Kind "api": any OpenAI-compatible server; Ollama's /v1 is a real one. A key is stored (Ollama ignores it).
        r = await admin.post(
            "/api/admin/providers",
            json={
                "name": "Local API",
                "slug": "local-api",
                "kind": "api",
                "base_url": "http://127.0.0.1:11434/v1",
                "self_hosted": True,
                "api_key": "not-a-real-key-for-ollama",
                "daily_request_limit": 3,
            },
        )
        assert r.status_code == 201, r.text
        api = r.json()
        created.append(api["id"])
        assert api["locality"] == "local" and api["key_source"] == "vault" and api["metered"]
        test = (await admin.post(f"/api/admin/providers/{api['id']}/test")).json()
        assert test["ok"], test
        found = (await admin.post(f"/api/admin/providers/{api['id']}/discover")).json()["models"]
        assert QWEN in [m["model"] for m in found]
        # A model id can always be typed by hand as well.
        assert (await admin.post(f"/api/admin/providers/{api['id']}/models", json={"model": QWEN})).status_code == 201

        # The API provider's model becomes the default: the user's chat goes there without picking a model.
        r = await admin.put("/api/admin/models/default", json={"model_id": f"local-api:{QWEN}"})
        assert r.status_code == 200
        models = (await hire.get("/api/models")).json()
        default = [m for m in models if m["is_default"]]
        assert [m["id"] for m in default] == [f"local-api:{QWEN}"] and default[0]["locality"] == "local"

        events = await _chat(hire, PUBLIC_Q)
        route = next(e for e in events if e["type"] == "route")
        assert route["model"] == f"local-api:{QWEN}", events
        done = events[-1]
        assert done["type"] == "done" and done["model"] == f"local-api:{QWEN}" and done["answer"].strip()

        # A user may still pick another cleared model explicitly.
        events = await _chat(hire, PUBLIC_Q, model=f"lan-ollama:{QWEN}")
        assert next(e for e in events if e["type"] == "route")["model"] == f"lan-ollama:{QWEN}"

        # Metering: requests counted for the API provider (not for the free local Ollama), cost recorded as
        # reported (Ollama reports none: 0 USD), and the daily cap stops the provider before anything is sent.
        async with get_sessionmaker()() as db:
            assert await requests_today(db, "local-api") == 1
            spend = await db.get(ProviderSpend, ("local-api", month_key()))
            assert spend is not None and spend.requests == 1 and spend.cost_usd == 0.0
            assert await requests_today(db, "lan-ollama") == 0
        assert (
            await admin.patch(f"/api/admin/providers/{api['id']}", json={"daily_request_limit": 1})
        ).status_code == 200
        events = await _chat(hire, PUBLIC_Q)
        assert any(e["type"] == "error" and "Daily request limit" in e["message"] for e in events), events
        assert not any(e["type"] == "delta" for e in events)

        async with get_sessionmaker()() as db:
            admin_id = await db.scalar(select(User.id).where(User.email == "admin@acme.test"))
            actions = {a.action for a in (await db.scalars(select(AuditLog).where(AuditLog.user_id == admin_id))).all()}
            assert {
                "admin.provider.create",
                "admin.provider.test",
                "admin.provider.discover",
                "admin.model.default",
            } <= actions
            uses = (await db.scalars(select(AuditLog).where(AuditLog.action == "secret.use"))).all()
            assert any(u.details.get("consumer") == "provider:local-api" for u in uses)
    finally:
        await admin.put("/api/admin/models/default", json={"model_id": None})
        for pid in created:
            await admin.delete(f"/api/admin/providers/{pid}")
        await admin.aclose()
        await hire.aclose()
        invalidate_router_cache()

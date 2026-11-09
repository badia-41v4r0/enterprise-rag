"""OpenRouter Jev Router as a virtual model of the OpenRouter provider (PLAN-v0.3 R9, ADR 0032).

OpenRouter itself is never called here: the payload is observed at the egress hook, which blocks the request
before it leaves the process (the project budget allows no paid or unpriced calls). What is asserted: the
plugin options follow OpenRouter's documentation, the virtual model is cleared for public data only by
default, a per-model clearance can only lower the provider's, and the options need step-up.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import registry
from app.llm.providers.base import EgressBlocked, EgressRecord, add_egress_hook, remove_egress_hook
from app.llm.router import invalidate_router_cache, load_router
from app.models import Classification, LlmModel, LlmProvider, Session
from app.services import vault
from tests.factories import login, make_user

JEV = registry.JEV_ROUTER_MODEL


def test_jev_router_options_follow_the_documentation() -> None:
    ok = registry.validate_jev_router_options(
        {"cost_tier": "low", "allowed_models": ["meta-llama/*:free", "meta-llama/*:free"], "excluded_models": []}
    )
    assert ok == {"cost_tier": "low", "allowed_models": ["meta-llama/*:free"]}
    assert registry.jev_router_plugin({}) == {"id": "jev-router"}  # Jev's default policy
    for bad in (
        {"cost_tier": "cheapest"},
        {"allowed_model": ["x"]},  # unknown field: OpenRouter answers 400
        {"allowed_models": "x"},
        {"allowed_models": ["x" * 1025]},
        {"excluded_models": ["a"] * 1025},
        [],
    ):
        with pytest.raises(registry.ProviderConfigError):
            registry.validate_jev_router_options(bad)


@pytest.fixture
def blocked_egress() -> Iterator[list[EgressRecord]]:
    records: list[EgressRecord] = []

    async def guard(rec: EgressRecord) -> None:
        records.append(rec)
        raise EgressBlocked("blocked by the test: OpenRouter is never called")

    add_egress_hook(guard)
    yield records
    remove_egress_hook(guard)


@pytest.mark.db
async def test_virtual_model_payload_clearance_and_step_up(
    db: AsyncSession, client: httpx.AsyncClient, blocked_egress: list[EgressRecord]
) -> None:
    admin = await make_user(db, "admin@acme.test", admin=True)
    await db.commit()
    client.headers["X-CSRF-Token"] = await login(client, "admin@acme.test")
    listing = (await client.get("/api/admin/providers")).json()
    orow = next(p for p in listing["providers"] if p["slug"] == "openrouter")
    # Added before any key exists, so no price lookup is made (no request to OpenRouter at all in this test).
    r = await client.post(f"/api/admin/providers/{orow['id']}/models", json={"model": JEV})
    assert r.status_code == 201, r.text
    await vault.set_secret(db, registry.OPENROUTER_SECRET, "not-a-real-key", user_id=admin.id)
    await db.commit()
    model = r.json()
    assert model["is_jev_router"] and model["requested_classification"] is None
    r = await client.patch(
        f"/api/admin/providers/{orow['id']}/models/{model['id']}",
        json={"jev_router": {"cost_tier": "low", "allowed_models": ["meta-llama/llama-3.3-8b-instruct:free"]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["jev_router"]["cost_tier"] == "low"
    bad = await client.patch(f"/api/admin/providers/{orow['id']}/models/{model['id']}", json={"jev_router": {"x": 1}})
    assert bad.status_code == 422
    # A model can only lower its provider's clearance (OpenRouter: internal at most here).
    r = await client.patch(
        f"/api/admin/providers/{orow['id']}/models/{model['id']}", json={"max_classification": "confidential"}
    )
    assert r.status_code == 422
    invalidate_router_cache()
    router = await load_router(db)
    jev = router.get(f"openrouter:{JEV}")
    plain = router.get(f"openrouter:{orow['models'][0]['model']}")
    assert jev is not None and plain is not None
    assert jev.max_classification == Classification.public  # the virtual model sees the whole conversation
    assert plain.max_classification == Classification.internal
    # The payload carries the documented plugin; the request is stopped at the egress hook.
    with pytest.raises(EgressBlocked):
        async for _ in jev.stream_chat([{"role": "user", "content": "hi"}], classification=Classification.public):
            pass
    payload = blocked_egress[0].payload
    assert payload["model"] == JEV and payload["usage"] == {"include": True}
    assert payload["plugins"] == [
        {"id": "jev-router", "cost_tier": "low", "allowed_models": ["meta-llama/llama-3.3-8b-instruct:free"]}
    ]
    # Internal data never reaches it, whatever the provider's clearance.
    with pytest.raises(Exception, match="cleared for public"):
        async for _ in jev.stream_chat([{"role": "user", "content": "x"}], classification=Classification.internal):
            pass
    assert len(blocked_egress) == 1
    stored = await db.scalar(select(LlmModel).join(LlmProvider).where(LlmModel.model == JEV))
    assert stored is not None and stored.options["jev_router"]["cost_tier"] == "low"
    # Changing the options or a model's clearance needs a recent re-authentication.
    stale = datetime.now(UTC) - timedelta(minutes=10)
    await db.execute(update(Session).where(Session.user_id == admin.id).values(reauth_at=stale))
    await db.commit()
    for body in ({"jev_router": {}}, {"max_classification": "public"}, {"supports_tools": False}):
        r = await client.patch(f"/api/admin/providers/{orow['id']}/models/{model['id']}", json=body)
        assert r.status_code == 403 and r.json()["detail"] == "step_up_required", body
    invalidate_router_cache()

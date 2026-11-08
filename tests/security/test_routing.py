"""Routing tests: confidential context never reaches a cloud provider.

The real OpenRouter provider code runs; an egress hook records every outgoing request and
blocks it before it leaves the machine (ADR 0008). Nothing answers: no fake model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import effective_permissions
from app.ingestion.embedder import OllamaEmbedder
from app.llm.providers.base import (
    ClassificationViolation,
    EgressBlocked,
    EgressRecord,
    add_egress_hook,
    remove_egress_hook,
)
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.router import ModelRouter, RoutingRefused
from app.models import Classification
from app.orchestrator.chat import ChatOrchestrator
from demo.acme.seed import PERSONAS
from tests.security.conftest import CANARY_BY_SLUG, user


def cloud(max_cls: Classification = Classification.internal) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="some/cloud-model",
        locality="cloud",
        max_classification=max_cls,
        api_key="not-a-real-key",
    )


def local() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3:4b",
        locality="local",
        max_classification=Classification.confidential,
    )


@pytest.fixture
async def egress() -> AsyncIterator[list[EgressRecord]]:
    records: list[EgressRecord] = []

    async def guard(rec: EgressRecord) -> None:
        records.append(rec)
        raise EgressBlocked("blocked by test egress guard")

    add_egress_hook(guard)
    yield records
    remove_egress_hook(guard)


# --- pure policy ----------------------------------------------------------------------------


def test_router_policy() -> None:
    r = ModelRouter([cloud(), local()])
    assert r.choose(Classification.confidential).locality == "local"
    assert r.choose(Classification.confidential, requested="openrouter:some/cloud-model").locality == "local"
    assert r.choose(Classification.internal, requested="openrouter:some/cloud-model").locality == "cloud"
    assert r.choose(Classification.public).locality == "local"  # local preferred by default
    with pytest.raises(RoutingRefused):
        ModelRouter([cloud()]).choose(Classification.confidential)


def test_cloud_provider_can_never_be_cleared_for_confidential() -> None:
    assert cloud(Classification.confidential).max_classification == Classification.internal


async def test_provider_refuses_above_clearance_before_any_egress(egress: list[EgressRecord]) -> None:
    with pytest.raises(ClassificationViolation):
        async for _ in cloud().stream_chat(
            [{"role": "user", "content": "x"}], classification=Classification.confidential
        ):
            pass
    assert egress == []


# --- end to end through the orchestrator -----------------------------------------------------


@pytest.mark.db
@pytest.mark.embeddings
async def test_confidential_context_never_reaches_openrouter(session: AsyncSession, egress: list[EgressRecord]) -> None:
    hr = next(p for p in PERSONAS if p.email == "hr@acme.test")
    u = await user(session, hr)
    perms = await effective_permissions(session, u)

    # Only a cloud model configured: the request is refused, nothing leaves.
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter([cloud()]))
    events = [
        e
        async for e in orch.run(
            u, perms, "What is the salary band for a senior engineer?", requested_model="openrouter:some/cloud-model"
        )
    ]
    assert any(e["type"] == "refusal" for e in events)
    assert egress == []

    # Local + cloud, user asks for the cloud model: routed to local anyway.
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter([cloud(), local()]))
    events = [
        e
        async for e in orch.run(
            u, perms, "What is the salary band for a senior engineer?", requested_model="openrouter:some/cloud-model"
        )
    ]
    assert [r.provider for r in egress] == ["ollama"]
    assert egress[0].classification == Classification.confidential
    assert "CANARY-HR-9XK3" in str(egress[0].payload)  # the local request did carry the context


@pytest.mark.db
@pytest.mark.embeddings
async def test_internal_context_may_use_cloud_and_carries_only_authorized_data(
    session: AsyncSession, egress: list[EgressRecord]
) -> None:
    eng = next(p for p in PERSONAS if p.email == "engineer@acme.test")
    u = await user(session, eng)
    perms = await effective_permissions(session, u)
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter([cloud(), local()]))
    _ = [
        e
        async for e in orch.run(
            u, perms, "When are we allowed to deploy to production?", requested_model="openrouter:some/cloud-model"
        )
    ]
    assert [r.provider for r in egress] == ["openrouter"]
    rec = egress[0]
    assert rec.classification == Classification.internal and rec.locality == "cloud"
    body = str(rec.payload)
    assert CANARY_BY_SLUG["engineering"] in body
    for slug in ("hr-confidential", "finance"):
        assert CANARY_BY_SLUG[slug] not in body


# --- one case per classification, with the ACME data (public / internal / confidential) -------------


@pytest.mark.parametrize(
    ("ceiling", "classification", "cloud_allowed"),
    [
        (Classification.public, Classification.public, True),
        (Classification.public, Classification.internal, False),
        (Classification.public, Classification.confidential, False),
        (Classification.internal, Classification.public, True),
        (Classification.internal, Classification.internal, True),
        (Classification.internal, Classification.confidential, False),
    ],
)
def test_cloud_ceiling_matrix(ceiling: Classification, classification: Classification, cloud_allowed: bool) -> None:
    """ERAG_CLOUD_MAX_CLASSIFICATION decides how far the cloud may go; confidential is never cloud."""
    assert bool(ModelRouter([cloud(ceiling)]).permitted(classification)) is cloud_allowed


@pytest.mark.db
@pytest.mark.embeddings
async def test_public_context_may_use_cloud_even_with_the_strictest_ceiling(
    session: AsyncSession, egress: list[EgressRecord]
) -> None:
    newhire = next(p for p in PERSONAS if p.email == "newhire@acme.test")
    u = await user(session, newhire)
    perms = await effective_permissions(session, u)
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter([cloud(Classification.public), local()]))
    _ = [
        e
        async for e in orch.run(
            u,
            perms,
            "What is new in version 4.2 of the ACME Cloud Planner mobile app?",
            requested_model="openrouter:some/cloud-model",
        )
    ]
    seen = {k: v in str(egress[0].payload) for k, v in CANARY_BY_SLUG.items()}
    assert [r.provider for r in egress] == ["openrouter"], (egress[0].classification, seen)
    assert egress[0].classification == Classification.public
    assert CANARY_BY_SLUG["product-docs"] in str(egress[0].payload)


@pytest.mark.db
@pytest.mark.embeddings
async def test_internal_context_stays_local_when_the_cloud_ceiling_is_public(
    session: AsyncSession, egress: list[EgressRecord]
) -> None:
    eng = next(p for p in PERSONAS if p.email == "engineer@acme.test")
    u = await user(session, eng)
    perms = await effective_permissions(session, u)
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter([cloud(Classification.public), local()]))
    _ = [
        e
        async for e in orch.run(
            u, perms, "When are we allowed to deploy to production?", requested_model="openrouter:some/cloud-model"
        )
    ]
    assert [r.provider for r in egress] == ["ollama"]
    assert egress[0].classification == Classification.internal

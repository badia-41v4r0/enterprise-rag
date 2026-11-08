"""Revocation scenario: the admin removes a role live; the next question loses access, and earlier
answers grounded in now-forbidden documents are withheld from the conversation history."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import effective_permissions
from app.ingestion.embedder import OllamaEmbedder
from app.llm.router import ModelRouter, build_providers
from app.main import create_app
from app.models import Classification, Message, Role
from app.orchestrator.chat import WITHHELD, ChatOrchestrator
from demo.acme.seed import PERSONAS
from tests.security.conftest import DEMO_PASSWORD, user

pytestmark = [pytest.mark.db, pytest.mark.embeddings]

Q = "What is the salary band for a senior engineer?"


async def test_role_removal_is_immediate_and_history_is_withheld(session: AsyncSession) -> None:
    hr = next(p for p in PERSONAS if p.email == "hr@acme.test")
    u = await user(session, hr)
    uid = u.id
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter(build_providers()))

    perms = await effective_permissions(session, u)
    prep = await orch.prepare(u, perms, Q)
    assert "CANARY-HR-9XK3" in "\n".join(m["content"] for m in prep.messages)

    # Simulate an earlier, persisted answer in this conversation grounded in the HR document.
    conv = prep.conversation
    conv_id = conv.id
    session.add(Message(conversation_id=conv.id, role="user", content=Q))
    session.add(
        Message(
            conversation_id=conv.id,
            role="assistant",
            content="Senior Engineer (E3): 58,000 to 72,000 EUR [1] CANARY-HR-9XK3",
            citations={"items": [i.citation() for i in prep.context.items]},
            classification=Classification.confidential,
            model_used="ollama:qwen3:4b",
        )
    )
    await session.commit()

    # Admin removes the HR role from the user through the real admin API.
    roles = {r.name: r.id for r in (await session.scalars(select(Role))).all()}
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as admin:
        r = await admin.post("/api/auth/login", json={"email": "admin@acme.test", "password": DEMO_PASSWORD})
        admin.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        r = await admin.patch(f"/api/admin/users/{uid}", json={"role_ids": [str(roles["Employee"])]})
        assert r.status_code == 200, r.text

    session.expire_all()
    u = await user(session, hr)
    perms = await effective_permissions(session, u)
    prep2 = await orch.prepare(u, perms, Q, conversation_id=conv_id)
    text = "\n".join(m["content"] for m in prep2.messages)
    assert "CANARY-HR-9XK3" not in text  # neither from retrieval nor from history
    assert "58,000" not in text
    assert WITHHELD in text
    assert prep2.withheld_message_ids
    assert prep2.history_classification != Classification.confidential
    assert all(i.classification != Classification.confidential for i in prep2.context.items)

    # Restore the role for other tests in this module.
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as admin:
        r = await admin.post("/api/auth/login", json={"email": "admin@acme.test", "password": DEMO_PASSWORD})
        admin.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        await admin.patch(f"/api/admin/users/{uid}", json={"role_ids": [str(roles["Employee"]), str(roles["HR"])]})


async def test_confidential_history_keeps_routing_local(session: AsyncSession) -> None:
    """A conversation that already contains confidential answers stays on local models."""
    hr = next(p for p in PERSONAS if p.email == "hr@acme.test")
    u = await user(session, hr)
    perms = await effective_permissions(session, u)
    orch = ChatOrchestrator(session, OllamaEmbedder(), ModelRouter(build_providers()))
    prep = await orch.prepare(u, perms, Q)
    session.add(
        Message(
            conversation_id=prep.conversation.id,
            role="assistant",
            content="earlier confidential answer",
            citations={"items": [i.citation() for i in prep.context.items]},
            classification=Classification.confidential,
        )
    )
    await session.flush()
    prep2 = await orch.prepare(u, perms, "Which plans does ACME Cloud Planner offer?", prep.conversation.id)
    assert prep2.history_classification == Classification.confidential
    assert prep2.classification == Classification.confidential

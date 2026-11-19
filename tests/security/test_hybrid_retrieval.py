"""Hybrid retrieval (R14): the full-text leg is a new path to the data, so it must apply exactly the same
authorization as the vector leg; fusion, the reranker and its degraded mode must never widen what a user sees.

No embedding model is involved: chunks get explicit vectors (as in test_vector_filter) and real tsvectors.
"""

from __future__ import annotations

import random
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import can_read_document, effective_permissions
from app.models import Action, AuditLog, Classification, Container, ResourceType
from app.rag.lexical import index_version_text
from app.rag.retrieval import Retriever
from app.rag.settings import RetrievalSettings
from app.rag.vector_store import AccessFilter, PgVectorStore
from tests.factories import grant, login, make_container, make_role, make_user
from tests.security.test_vector_filter import add_doc, vec

pytestmark = pytest.mark.db


async def add_text_doc(
    db: AsyncSession, c: Container, title: str, seed: int, body: str, *, restricted: bool = False
) -> uuid.UUID:
    d, chunks = await add_doc(db, c, title, seed, restricted=restricted, n_chunks=1)
    chunks[0].text = body
    await db.flush()
    await index_version_text(db, chunks[0].version_id, "english")
    return d.id


@pytest.fixture
async def world(db: AsyncSession) -> dict[str, object]:
    sales, staff = await make_role(db, "sales"), await make_role(db, "staff")
    c_sales = await make_container(db, "Sales", Classification.confidential)
    c_pub = await make_container(db, "Public", Classification.public)
    contract = await add_text_doc(db, c_sales, "contract", 11, "Contract CT-2025-0137: payment term 60 days.")
    secret = await add_text_doc(
        db, c_sales, "settlement", 12, "Settlement CT-2025-0137 confidential amount.", restricted=True
    )
    public = await add_text_doc(db, c_pub, "catalog", 13, "Catalog AX-1088 sensor weight 120 g.")
    await grant(db, sales, ResourceType.container, c_sales.id, Action.read)
    await grant(db, staff, ResourceType.container, c_pub.id, Action.read)
    await grant(db, sales, ResourceType.container, c_pub.id, Action.read)
    u_sales = await make_user(db, "sales@x.test", roles=[sales])
    u_staff = await make_user(db, "staff@x.test", roles=[staff])
    await db.commit()
    return {
        "c_sales": c_sales,
        "c_pub": c_pub,
        "contract": contract,
        "secret": secret,
        "public": public,
        "sales": u_sales,
        "staff": u_staff,
    }


async def test_full_text_leg_applies_the_same_authorization(db: AsyncSession, world: dict[str, object]) -> None:
    store = PgVectorStore(db)
    staff = await effective_permissions(db, world["staff"])  # type: ignore[arg-type]
    sales = await effective_permissions(db, world["sales"])  # type: ignore[arg-type]
    # Only the full-text leg can find an exact contract number; a user without the container gets nothing.
    assert await store.search_text("CT-2025-0137", AccessFilter.from_permissions(staff), 8) == []
    hits = await store.search_text("CT-2025-0137", AccessFilter.from_permissions(sales), 8)
    # Container read is not enough for the restricted settlement.
    assert [h.document_id for h in hits] == [world["contract"]]
    assert {h.document_id for h in await store.search_text("AX-1088", AccessFilter.from_permissions(staff), 8)} == {
        world["public"]
    }


async def test_property_every_leg_and_the_fusion_respect_the_policy(db: AsyncSession) -> None:
    """Random containers, documents (some restricted) and users: every hit of each leg, and of the full
    pipeline, must be readable according to the policy."""
    rnd = random.Random(7)
    containers = [await make_container(db, f"C{i}", Classification.internal) for i in range(6)]
    words = ["alpha", "bravo", "charlie", "delta", "echo", "INV-2026-0101", "CT-2025-0001"]
    for i in range(60):
        c = rnd.choice(containers)
        body = " ".join(rnd.choice(words) for _ in range(8))
        await add_text_doc(db, c, f"doc{i}", 100 + i, body, restricted=rnd.random() < 0.2)
    users = []
    for j in range(5):
        role = await make_role(db, f"r{j}")
        for c in rnd.sample(containers, rnd.randint(1, 4)):
            await grant(db, role, ResourceType.container, c.id, Action.read)
        users.append(await make_user(db, f"u{j}@x.test", roles=[role]))
    await db.commit()

    store = PgVectorStore(db)
    retriever = Retriever(db, embedder=None)  # type: ignore[arg-type]  # vectors are passed explicitly
    cfg = RetrievalSettings(mode="hybrid", reranker="", vector_min_score=0.0, text_min_rank=0.0, final_k=20)
    for u in users:
        perms = await effective_permissions(db, u)
        access = AccessFilter.from_permissions(perms)
        for q in ["alpha delta", "INV-2026-0101", "CT-2025-0001 echo"]:
            qv = vec(rnd.randint(0, 10_000))
            legs = [*await store.search(qv, access, 20), *await store.search_text(q, access, 20)]
            full = (await retriever.retrieve(perms, q, settings=cfg, vector=qv)).hits
            for h in legs + full:
                assert can_read_document(
                    perms, container_id=h.container_id, document_id=h.document_id, restricted=h.restricted
                ), f"{u.email} got {h.title}"


async def test_missing_reranker_falls_back_without_widening(
    db: AsyncSession, world: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from app.core.config import get_settings
    from app.rag import reranker

    monkeypatch.setenv("ERAG_MODELS_DIR", str(tmp_path))  # no model installed there
    get_settings.cache_clear()
    reranker.reset()  # forget a model loaded by an earlier test in this process
    try:
        perms = await effective_permissions(db, world["staff"])  # type: ignore[arg-type]
        cfg = RetrievalSettings(mode="hybrid", reranker="mmarco-minilm-l12", text_min_rank=0.0, vector_min_score=0.0)
        res = await Retriever(db, embedder=None).retrieve(  # type: ignore[arg-type]
            perms, "CT-2025-0137 AX-1088", settings=cfg, vector=vec(13 * 100)
        )
        assert res.reranker_fallback is not None and "not installed" in res.reranker_fallback
        assert not res.reranked
        assert {h.document_id for h in res.hits} == {world["public"]}
    finally:
        get_settings.cache_clear()
        reranker.reset()


async def test_retrieval_settings_api_is_admin_only_validated_and_audited(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await make_user(db, "admin@x.test", admin=True)
    await make_user(db, "user@x.test")
    await db.commit()
    await login(client, "user@x.test")
    assert (await client.get("/api/admin/retrieval")).status_code == 403
    await login(client, "admin@x.test")
    r = await client.get("/api/admin/retrieval")
    assert r.status_code == 200 and r.json()["settings"]["mode"] == "hybrid"
    assert any(m["name"] == "mmarco-minilm-l12" for m in r.json()["models"])
    assert (await client.put("/api/admin/retrieval", json={"mode": "magic"})).status_code == 422
    assert (await client.put("/api/admin/retrieval", json={"reranker": "unknown"})).status_code == 422
    r = await client.put("/api/admin/retrieval", json={"mode": "vector", "final_k": 6})
    assert r.status_code == 200 and r.json()["settings"]["mode"] == "vector" and r.json()["settings"]["final_k"] == 6
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.retrieval.update"))
    assert entry is not None and entry.details["changed"]["mode"] == ["hybrid", "vector"]

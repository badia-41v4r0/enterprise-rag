"""The ACL filter is applied inside the vector query, and re-checked afterwards.

No model is involved: chunks get explicit test vectors, and the query vector is chosen to be
*identical* to an unauthorized chunk, so an unfiltered search would rank it first.
"""

from __future__ import annotations

import math
import random
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import effective_permissions
from app.models import (
    EMBED_DIM,
    Action,
    Chunk,
    Classification,
    Container,
    Document,
    DocumentVersion,
    Grant,
    ResourceType,
    User,
    VersionStatus,
)
from app.rag.retrieval import Retriever, recheck
from app.rag.vector_store import AccessFilter, PgVectorStore
from tests.factories import grant, make_container, make_role, make_user

pytestmark = pytest.mark.db


def vec(seed: int) -> list[float]:
    rnd = random.Random(seed)
    v = [rnd.gauss(0, 1) for _ in range(EMBED_DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


async def add_doc(
    db: AsyncSession, c: Container, title: str, seed: int, *, restricted: bool = False, n_chunks: int = 2
) -> tuple[Document, list[Chunk]]:
    d = Document(container_id=c.id, title=title, filename=f"{title}.md", restricted=restricted)
    db.add(d)
    await db.flush()
    v = DocumentVersion(document_id=d.id, sha256=uuid.uuid4().hex * 2, blob_key="x", status=VersionStatus.ready)
    db.add(v)
    await db.flush()
    d.current_version_id = v.id
    chunks = [
        Chunk(
            version_id=v.id,
            document_id=d.id,
            container_id=c.id,
            ordinal=i,
            text=f"{title} chunk {i}",
            token_count=5,
            embedding=vec(seed * 100 + i),
            meta={},
        )
        for i in range(n_chunks)
    ]
    db.add_all(chunks)
    await db.flush()
    return d, chunks


@pytest.fixture
async def world(db: AsyncSession) -> dict[str, object]:
    eng_role, hr_role = await make_role(db, "eng"), await make_role(db, "hr")
    c_eng = await make_container(db, "Eng", Classification.internal)
    c_hr = await make_container(db, "HR", Classification.confidential)
    _eng_doc, eng_chunks = await add_doc(db, c_eng, "eng-doc", 1)
    _hr_doc, hr_chunks = await add_doc(db, c_hr, "hr-doc", 2)
    secret_doc, secret_chunks = await add_doc(db, c_hr, "hr-secret", 3, restricted=True)
    await grant(db, eng_role, ResourceType.container, c_eng.id)
    hr_container_grant = await grant(db, hr_role, ResourceType.container, c_hr.id)
    engineer = await make_user(db, "eng@acme.test", roles=[eng_role])
    hr = await make_user(db, "hr@acme.test", roles=[hr_role])
    await db.commit()
    return {
        "engineer": engineer,
        "hr": hr,
        "hr_role": hr_role,
        "hr_grant": hr_container_grant,
        "eng_chunks": eng_chunks,
        "hr_chunks": hr_chunks,
        "secret_doc": secret_doc,
        "secret_chunks": secret_chunks,
        "c_hr": c_hr,
    }


def ids(hits: list) -> set[uuid.UUID]:
    return {h.chunk_id for h in hits}


async def test_unauthorized_chunk_never_returned_even_when_it_is_the_exact_match(db: AsyncSession, world: dict) -> None:
    perms = await effective_permissions(db, world["engineer"])
    target = world["hr_chunks"][0]
    hits = await PgVectorStore(db).search(vec(2 * 100 + 0), AccessFilter.from_permissions(perms), k=50)
    assert target.id not in ids(hits)
    assert ids(hits) == {c.id for c in world["eng_chunks"]}


async def test_restricted_document_needs_document_grant(db: AsyncSession, world: dict) -> None:
    hr: User = world["hr"]
    perms = await effective_permissions(db, hr)
    hits = await PgVectorStore(db).search(vec(3 * 100), AccessFilter.from_permissions(perms), k=50)
    assert ids(hits) == {c.id for c in world["hr_chunks"]}  # container read, but not the restricted doc

    await grant(db, world["hr_role"], ResourceType.document, world["secret_doc"].id, Action.read)
    await db.commit()
    perms = await effective_permissions(db, hr)
    hits = await PgVectorStore(db).search(vec(3 * 100), AccessFilter.from_permissions(perms), k=50)
    assert {c.id for c in world["secret_chunks"]} <= ids(hits)


async def test_empty_permissions_return_nothing(db: AsyncSession, world: dict) -> None:
    nobody = await make_user(db, "nobody@acme.test")
    await db.commit()
    perms = await effective_permissions(db, nobody)
    assert await PgVectorStore(db).search(vec(1), AccessFilter.from_permissions(perms), k=50) == []


async def test_revocation_is_immediate_without_reindex(db: AsyncSession, world: dict) -> None:
    hr: User = world["hr"]
    retriever = Retriever(db, embedder=None)  # vector path only
    perms = await effective_permissions(db, hr)
    before = await retriever.retrieve_by_vector(perms, vec(200), k=10, min_score=-1.0)
    assert ids(before.hits) == {c.id for c in world["hr_chunks"]}

    await db.execute(delete(Grant).where(Grant.id == world["hr_grant"].id))
    await db.commit()
    perms = await effective_permissions(db, hr)
    after = await retriever.retrieve_by_vector(perms, vec(200), k=10, min_score=-1.0)
    assert after.hits == []


async def test_only_current_version_is_searchable(db: AsyncSession, world: dict) -> None:
    perms = await effective_permissions(db, world["engineer"])
    eng_chunk: Chunk = world["eng_chunks"][0]
    doc = await db.get(Document, eng_chunk.document_id)
    assert doc is not None
    v2 = DocumentVersion(document_id=doc.id, sha256="b" * 64, blob_key="y", status=VersionStatus.ready)
    db.add(v2)
    await db.flush()
    doc.current_version_id = v2.id  # old chunks still exist but are not current
    await db.commit()
    hits = await PgVectorStore(db).search(vec(100), AccessFilter.from_permissions(perms), k=50)
    assert eng_chunk.id not in ids(hits)


async def test_acl_recheck_drops_hits_that_changed_after_search(db: AsyncSession, world: dict) -> None:
    hr: User = world["hr"]
    perms = await effective_permissions(db, hr)
    hits = await PgVectorStore(db).search(vec(200), AccessFilter.from_permissions(perms), k=10)
    assert hits
    # Between search and use, the document is flagged restricted (or moved): the re-check must drop it.
    doc = await db.get(Document, hits[0].document_id)
    assert doc is not None
    doc.restricted = True
    await db.commit()
    result = await recheck(db, perms, hits)
    assert hits[0].chunk_id in result.dropped_chunk_ids
    assert all(h.document_id != doc.id for h in result.hits)

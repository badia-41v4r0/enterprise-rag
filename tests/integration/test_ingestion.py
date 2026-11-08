"""Upload -> job -> worker -> chunks, against real Postgres and (where marked) real bge-m3."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.ingestion.blobstore import get_blobstore
from app.ingestion.worker import claim_job, drain
from app.models import (
    Action,
    Chunk,
    Classification,
    Document,
    DocumentVersion,
    IngestionJob,
    JobState,
    ResourceType,
    VersionStatus,
)
from app.services.documents import upload_document
from tests.factories import grant, login, make_container, make_role, make_user

pytestmark = pytest.mark.db


async def _setup(db: AsyncSession) -> tuple[str, str]:
    mgr_role, reader_role = await make_role(db, "eng-lead"), await make_role(db, "eng")
    c = await make_container(db, "Engineering", Classification.internal)
    await grant(db, mgr_role, ResourceType.container, c.id, Action.manage)
    await grant(db, reader_role, ResourceType.container, c.id, Action.read)
    await make_user(db, "lead@acme.test", roles=[mgr_role])
    await make_user(db, "dev@acme.test", roles=[reader_role])
    await db.commit()
    return str(c.id), "lead@acme.test"


async def _count_chunks(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(Chunk)) or 0)


@pytest.mark.embeddings
async def test_upload_index_dedupe_and_new_version(db: AsyncSession, client: httpx.AsyncClient) -> None:
    cid, lead = await _setup(db)
    await login(client, lead)
    body = b"# Deploys\n\nWe deploy on Tuesdays.\n\n## Rollback\n\nUse the blue-green switch."
    r = await client.post(f"/api/containers/{cid}/documents", files={"file": ("deploys.md", body, "text/markdown")})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "queued"
    doc_id = r.json()["document_id"]

    assert await drain() == 1
    db.expire_all()
    n1 = await _count_chunks(db)
    assert n1 >= 1
    doc = await db.get(Document, r.json()["document_id"])
    assert doc is not None and str(doc.current_version_id) == r.json()["version_id"]

    # Identical file is skipped (even under another name).
    r2 = await client.post(f"/api/containers/{cid}/documents", files={"file": ("copy.md", body, "text/markdown")})
    assert r2.json()["status"] == "duplicate" and r2.json()["document_id"] == doc_id
    assert await drain() == 0

    # Same filename, new content => new version; only this document is re-processed, old vectors removed.
    other = await client.post(
        f"/api/containers/{cid}/documents", files={"file": ("other.txt", b"Unrelated text.", "text/plain")}
    )
    assert await drain() == 1
    db.expire_all()
    other_chunks = set(
        (await db.scalars(select(Chunk.id).where(Chunk.document_id == other.json()["document_id"]))).all()
    )

    body_v2 = body + b"\n\n## Freeze\n\nNo deploys in December."
    r3 = await client.post(f"/api/containers/{cid}/documents", files={"file": ("deploys.md", body_v2, "text/markdown")})
    assert r3.json()["status"] == "queued" and r3.json()["document_id"] == doc_id
    assert await drain() == 1
    db.expire_all()
    versions = {
        str(v.id): v.status
        for v in (await db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc_id))).all()
    }
    assert versions[r.json()["version_id"]] == VersionStatus.superseded
    assert versions[r3.json()["version_id"]] == VersionStatus.ready
    doc_chunk_versions = set((await db.scalars(select(Chunk.version_id).where(Chunk.document_id == doc_id))).all())
    assert {str(v) for v in doc_chunk_versions} == {r3.json()["version_id"]}
    # The other document's chunks were untouched.
    assert set((await db.scalars(select(Chunk.id).where(Chunk.document_id == other.json()["document_id"]))).all()) == (
        other_chunks
    )

    detail = (await client.get(f"/api/documents/{doc_id}")).json()
    assert len(detail["versions"]) == 2 and detail["chunk_count"] >= 1


async def test_upload_permissions_and_formats(db: AsyncSession, client: httpx.AsyncClient) -> None:
    cid, _ = await _setup(db)
    await login(client, "dev@acme.test")  # read only
    r = await client.post(f"/api/containers/{cid}/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 404
    client.cookies.clear()
    await login(client, "lead@acme.test")
    r = await client.post(f"/api/containers/{cid}/documents", files={"file": ("a.exe", b"MZ", "application/x")})
    assert r.status_code == 415
    r = await client.post(
        f"/api/containers/{cid}/documents", files={"file": ("../../etc/passwd.txt", b"x", "text/plain")}
    )
    assert r.status_code == 201
    doc = await db.get(Document, r.json()["document_id"])
    assert doc is not None and doc.filename == "passwd.txt"


async def test_skip_locked_claims_distinct_jobs(db: AsyncSession) -> None:
    c = await make_container(db, "C", Classification.public)
    await db.commit()
    for i in range(2):
        await upload_document(
            db, get_blobstore(), container=c, filename=f"f{i}.txt", data=f"doc {i}".encode(), user_id=None
        )
    sm = get_sessionmaker()
    async with sm() as s1, sm() as s2:
        j1 = await claim_job(s1)
        j2 = await claim_job(s2)  # s1 still holds its row lock
        assert j1 is not None and j2 is not None and j1.id != j2.id
        async with sm() as s3:
            assert await claim_job(s3) is None  # both rows locked or claimed
        await s1.commit()
        await s2.commit()
    states = (await db.scalars(select(IngestionJob.state))).all()
    assert all(s == JobState.running for s in states)


async def test_failed_parse_marks_version_failed(db: AsyncSession) -> None:
    c = await make_container(db, "C", Classification.public)
    await db.commit()
    res = await upload_document(db, get_blobstore(), container=c, filename="empty.txt", data=b"   ", user_id=None)
    await drain()
    db.expire_all()
    v = await db.get(DocumentVersion, res.version_id)
    assert v is not None and v.status == VersionStatus.failed and "no extractable text" in (v.error or "")
    job = await db.scalar(select(IngestionJob).where(IngestionJob.version_id == res.version_id))
    assert job is not None and job.state == JobState.failed

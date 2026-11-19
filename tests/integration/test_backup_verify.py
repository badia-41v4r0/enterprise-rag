"""`erag backup verify` (R19): it passes on a whole installation and fails, check by check, on what a broken
restore looks like (wrong key, missing or corrupt blobs, missing embeddings). It never prints secret values."""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Cipher, decode_key, generate_key
from app.ingestion.blobstore import LocalBlobStore, get_blobstore
from app.ingestion.worker import drain
from app.models import Chunk, Classification
from app.services import backup, vault
from app.services.documents import upload_document
from tests.factories import make_container
from tests.integration.test_ingestion_at_scale import FakeEmbedder

pytestmark = pytest.mark.db


async def installation(db: AsyncSession) -> str:
    await vault.set_secret(db, "connector_token", "s3cr3t-value", user_id=None)
    c = await make_container(db, "Docs", Classification.internal)
    up = await upload_document(
        db, get_blobstore(), container=c, filename="a.md", data=b"# A\n\nAlpha bravo charlie.", user_id=None
    )
    await db.commit()
    assert await drain(embedder=FakeEmbedder()) == 1
    return up.sha256


async def test_whole_installation_passes(db: AsyncSession) -> None:
    await installation(db)
    report = await backup.verify(db, get_blobstore(), full=True)
    assert report.ok, report.as_dict()
    assert {c.name for c in report.checks} == {"schema", "master_key", "blobs", "embeddings"}
    assert "s3cr3t-value" not in str(report.as_dict())


async def test_a_master_key_from_another_installation_fails(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await installation(db)
    other = Cipher(decode_key(generate_key()))
    monkeypatch.setattr(backup, "get_cipher", lambda: other)
    report = await backup.verify(db, get_blobstore())
    key = next(c for c in report.checks if c.name == "master_key")
    assert not report.ok and not key.ok and key.detail == "0/1 secrets decrypt"


async def test_missing_and_corrupt_blobs_fail(db: AsyncSession) -> None:
    digest = await installation(db)
    store = get_blobstore()
    assert isinstance(store, LocalBlobStore)
    path = store._path(digest)
    path.write_bytes(b"not the original bytes")
    blobs = next(c for c in (await backup.verify(db, store)).checks if c.name == "blobs")
    assert not blobs.ok and "1 corrupt" in blobs.detail
    path.unlink()
    blobs = next(c for c in (await backup.verify(db, store)).checks if c.name == "blobs")
    assert not blobs.ok and "1 missing" in blobs.detail


async def test_missing_embeddings_fail(db: AsyncSession) -> None:
    await installation(db)
    await db.execute(delete(Chunk))
    await db.commit()
    emb = next(c for c in (await backup.verify(db, get_blobstore())).checks if c.name == "embeddings")
    assert not emb.ok and "1 ready versions without chunks" in emb.detail

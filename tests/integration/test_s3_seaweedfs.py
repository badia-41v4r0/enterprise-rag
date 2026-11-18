"""The S3 blob store against a real S3-compatible server (SeaweedFS, Apache-2.0; MinIO is excluded, D8).

Runs when ERAG_TEST_S3_ENDPOINT is set, for example:

    docker run -d --rm --name erag-test-seaweedfs -p 127.0.0.1:18333:8333 \
        -v $PWD/tests/fixtures/seaweedfs-s3.json:/etc/s3.json:ro chrislusf/seaweedfs:3.90 \
        server -s3 -s3.config=/etc/s3.json -dir=/data
    ERAG_TEST_S3_ENDPOINT=http://127.0.0.1:18333 uv run pytest tests/integration/test_s3_seaweedfs.py
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from app.ingestion.blobstore import LocalBlobStore, migrate_blobs
from app.ingestion.s3 import S3BlobStore, S3Error

ENDPOINT = os.environ.get("ERAG_TEST_S3_ENDPOINT", "")
ACCESS = os.environ.get("ERAG_TEST_S3_ACCESS_KEY_ID", "erag-test-ak")
SECRET = os.environ.get("ERAG_TEST_S3_SECRET_ACCESS_KEY", "erag-test-sk-0123456789")

pytestmark = pytest.mark.skipif(not ENDPOINT, reason="ERAG_TEST_S3_ENDPOINT not set (no S3 server to test against)")


async def fresh_store(**kw: object) -> S3BlobStore:
    bucket = f"erag-test-{uuid.uuid4().hex[:12]}"
    s3 = S3BlobStore(ENDPOINT, bucket, ACCESS, SECRET, sse="", **kw)  # type: ignore[arg-type]
    r = await s3._request("PUT", s3._url())  # create the bucket (an operator task in production)
    assert r.status_code < 300, r.text
    return s3


async def test_round_trip_dedupe_and_delete() -> None:
    s3 = await fresh_store(prefix="blobs")
    data = os.urandom(300_000)
    key = hashlib.sha256(data).hexdigest()
    assert not await s3.exists(key)
    await s3.put(key, data)
    await s3.put(key, data)  # content-addressed: a second put is a no-op
    assert await s3.exists(key)
    assert await s3.get(key) == data
    await s3.delete(key)
    assert not await s3.exists(key)
    with pytest.raises(FileNotFoundError):
        await s3.get(key)


async def test_wrong_credentials_are_refused() -> None:
    s3 = await fresh_store()
    bad = S3BlobStore(ENDPOINT, s3.bucket, ACCESS, "not-the-secret", sse="")
    key = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(S3Error):
        await bad.put(key, b"x")


async def test_migration_from_local_storage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    local = LocalBlobStore(tmp_path)
    bodies = [os.urandom(1000 + i) for i in range(5)]
    for b in bodies:
        await local.put(hashlib.sha256(b).hexdigest(), b)
    s3 = await fresh_store()
    assert await migrate_blobs(local, s3) == (5, 0)
    assert await migrate_blobs(local, s3) == (0, 5)
    for b in bodies:
        assert await s3.get(hashlib.sha256(b).hexdigest()) == b

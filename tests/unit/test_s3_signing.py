"""SigV4 against AWS's published example, and the S3 blob store against an in-memory S3 double. The store is
also exercised against a real SeaweedFS when ERAG_TEST_S3_ENDPOINT is set (tests/integration/test_s3_seaweedfs.py)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from app.ingestion.blobstore import LocalBlobStore, migrate_blobs
from app.ingestion.s3 import EMPTY_SHA256, S3BlobStore, S3Error, sign_v4


def test_signature_matches_the_aws_documentation_example() -> None:
    # "Example: GET Object" from the AWS Signature Version 4 documentation for S3.
    headers = sign_v4(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {"Range": "bytes=0-9"},
        EMPTY_SHA256,
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.seen_headers: list[dict[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen_headers.append(dict(req.headers))
        assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
        path = req.url.path
        if req.method == "PUT":
            assert hashlib.sha256(req.content).hexdigest() == req.headers["x-amz-content-sha256"]
            self.objects[path] = req.content
            return httpx.Response(200)
        if req.method in ("GET", "HEAD"):
            if path not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, content=self.objects[path] if req.method == "GET" else b"")
        if req.method == "DELETE":
            self.objects.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)


def store(fake: FakeS3, **kw: object) -> S3BlobStore:
    return S3BlobStore(
        "http://s3.local:8333", "erag", "ak", "sk", prefix="blobs", transport=httpx.MockTransport(fake), **kw
    )  # type: ignore[arg-type]


async def test_put_get_exists_delete_with_encryption_and_hash_check() -> None:
    fake = FakeS3()
    s3 = store(fake)
    data = b"quarterly numbers"
    key = hashlib.sha256(data).hexdigest()
    assert not await s3.exists(key)
    await s3.put(key, data)
    put = next(h for h in fake.seen_headers if "x-amz-server-side-encryption" in h)
    assert put["x-amz-server-side-encryption"] == "AES256"
    assert f"/erag/blobs/{key}" in fake.objects
    assert await s3.get(key) == data
    fake.objects[f"/erag/blobs/{key}"] = b"tampered"
    with pytest.raises(S3Error, match="does not match its hash"):
        await s3.get(key)
    await s3.delete(key)
    assert not await s3.exists(key)
    with pytest.raises(ValueError):
        await s3.get("../../etc/passwd")


async def test_migrate_local_blobs_to_s3_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    local = LocalBlobStore(tmp_path)
    keys = []
    for body in (b"one", b"two", b"three"):
        k = hashlib.sha256(body).hexdigest()
        await local.put(k, body)
        keys.append(k)
    fake = FakeS3()
    assert await migrate_blobs(local, store(fake)) == (3, 0)
    assert await migrate_blobs(local, store(fake)) == (0, 3)
    assert sorted([await store(fake).get(k) for k in keys]) == [b"one", b"three", b"two"]

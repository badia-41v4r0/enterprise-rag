"""Blob storage behind an interface (ADR 0005). Blobs are content-addressed by SHA-256."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

_KEY = re.compile(r"^[0-9a-f]{64}$")


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):  # keys are sha256 hex; never user-controlled paths
            raise ValueError("invalid blob key")
        return self.root / key[:2] / key[2:4] / key

    def _put_sync(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, key, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, True)

    def list_keys(self) -> list[str]:
        """Every stored key (for `erag blobs migrate`)."""
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.glob("*/*/*") if _KEY.match(p.name))


def get_blobstore() -> BlobStore:
    s = get_settings()
    if s.blob_backend == "s3":
        from app.ingestion.s3 import S3BlobStore

        return S3BlobStore(
            s.s3_endpoint,
            s.s3_bucket,
            s.s3_access_key_id,
            s.s3_secret_access_key,
            region=s.s3_region,
            prefix=s.s3_prefix,
            sse=s.s3_sse,
        )
    if s.blob_backend != "local":
        raise ValueError(f"unknown blob backend {s.blob_backend!r} (local or s3)")
    return LocalBlobStore(s.blob_dir)


async def migrate_blobs(source: LocalBlobStore, target: BlobStore) -> tuple[int, int]:
    """Copy every local blob to `target`, verifying each one by its hash. Returns (copied, already there).
    Idempotent: rerun it after an interruption. The local files are left in place."""
    import hashlib

    copied = present = 0
    for key in source.list_keys():
        if await target.exists(key):
            present += 1
            continue
        data = await source.get(key)
        if hashlib.sha256(data).hexdigest() != key:
            raise ValueError(f"local blob {key[:12]} does not match its hash; not copied")
        await target.put(key, data)
        copied += 1
    return copied, present

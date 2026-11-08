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


def get_blobstore() -> BlobStore:
    return LocalBlobStore(get_settings().blob_dir)

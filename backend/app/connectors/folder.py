"""`folder` connector: files below an admin-configured root (ERAG_CONNECTOR_ROOTS), read-only.

The configured folder must resolve inside one of the roots. While walking, symlinks (files or
directories) are never followed, and every file is opened with O_NOFOLLOW and re-checked to be
inside the folder, so neither `..` nor a symlink can reach anything outside it.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.connectors.base import BaseConnector, ConnectorError, Fetched, Item, ItemSkipped, register, supported
from app.core.config import Settings


class FolderConfig(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Folder below one of ERAG_CONNECTOR_ROOTS")
    recursive: bool = True
    max_items: int = Field(default=500, ge=1, le=100_000)


def resolve_folder(path: str, roots: list[Path]) -> Path:
    """The real folder for a configured path, refused unless it is inside an allowed root."""
    if not roots:
        raise ConnectorError("no connector roots are configured (ERAG_CONNECTOR_ROOTS)")
    raw = Path(path)
    candidates = [raw] if raw.is_absolute() else [r / raw for r in roots]
    for cand in candidates:
        try:
            real = cand.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        for root in roots:
            try:
                root_real = root.resolve(strict=True)
            except OSError:
                continue
            if real.is_relative_to(root_real) and real.is_dir():
                return real
    raise ConnectorError(f"{path!r} is not a folder inside an allowed root ({', '.join(map(str, roots))})")


@register
class FolderConnector(BaseConnector):
    kind: ClassVar[str] = "folder"
    Config: ClassVar[type[BaseModel]] = FolderConfig
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Any, secrets: dict[str, str], settings: Settings) -> None:
        super().__init__(config, secrets, settings)
        self.config = FolderConfig.model_validate(config)
        self.settings = settings
        self.folder = resolve_folder(self.config.path, list(settings.connector_roots))
        self.max_bytes = settings.connector_max_item_mb * 1024 * 1024

    def _walk(self) -> list[Item]:
        items: list[Item] = []
        for dirpath, dirnames, filenames in os.walk(self.folder, followlinks=False):
            base = Path(dirpath)
            for d in list(dirnames):
                if (base / d).is_symlink():
                    self.skipped.append((str((base / d).relative_to(self.folder)), "symlinked folder not followed"))
                    dirnames.remove(d)
            if not self.config.recursive:
                dirnames.clear()
            dirnames.sort()
            for name in sorted(filenames):
                p = base / name
                rel = p.relative_to(self.folder).as_posix()
                st = p.lstat()
                if stat.S_ISLNK(st.st_mode):
                    self.skipped.append((rel, "symlink not followed"))
                    continue
                if not stat.S_ISREG(st.st_mode) or name.startswith("."):
                    continue
                if not supported(name):
                    self.skipped.append((rel, "unsupported file type"))
                    continue
                items.append(Item(external_id=rel, name=rel, version_hint=f"{st.st_mtime_ns}:{st.st_size}"))
        return items

    async def list_items(self) -> AsyncIterator[Item]:
        for item in await asyncio.to_thread(self._walk):
            yield item

    def _read(self, item: Item) -> bytes:
        target = (self.folder / item.external_id).absolute()
        # Lexical check first (no `..`), then a no-follow open and a check of what was opened.
        if ".." in Path(item.external_id).parts or not target.is_relative_to(self.folder):
            raise ItemSkipped("path outside the folder")
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ItemSkipped(f"cannot open: {exc.strerror}") from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ItemSkipped("not a regular file")
            if st.st_size > self.max_bytes:
                raise ItemSkipped(f"larger than {self.settings.connector_max_item_mb} MB")
            if not Path(os.path.realpath(target)).is_relative_to(self.folder):
                raise ItemSkipped("path outside the folder")
            return fh.read(self.max_bytes + 1)

    async def fetch(self, item: Item, previous_hint: str | None) -> Fetched:
        data = await asyncio.to_thread(self._read, item)
        return Fetched(data=data, version_hint=item.version_hint)

    async def test(self) -> str:
        items = await asyncio.to_thread(self._walk)
        return f"{self.folder}: {len(items)} supported files"

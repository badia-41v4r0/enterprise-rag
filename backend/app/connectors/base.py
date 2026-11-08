"""Connector interface. New kinds register themselves in KINDS without touching the sync core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.config import Settings
from app.ingestion.parsers import SUPPORTED


class ConnectorError(Exception):
    """A configuration or source problem that stops the whole sync (not a single item)."""


class ItemSkipped(Exception):
    """A single item that is refused (too large, unsupported type, symlink, ...)."""


@dataclass(frozen=True)
class Item:
    external_id: str  # stable id in the source (relative path, URL)
    name: str  # filename used to pick the parser (must carry a supported extension)
    version_hint: str | None = None  # mtime/size or ETag: if unchanged, the item is not fetched again


@dataclass(frozen=True)
class Fetched:
    data: bytes
    version_hint: str | None = None
    unchanged: bool = False  # the source said "not modified" (HTTP 304)


def supported(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in SUPPORTED


class BaseConnector(ABC):
    kind: ClassVar[str]
    Config: ClassVar[type[BaseModel]]
    # Config fields that name vault secrets (they must also be assigned to the connector).
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Any, secrets: dict[str, str], settings: Settings) -> None:
        self.settings = settings
        self.skipped: list[tuple[str, str]] = []

    @abstractmethod
    def list_items(self) -> AsyncIterator[Item]: ...

    @abstractmethod
    async def fetch(self, item: Item, previous_hint: str | None) -> Fetched: ...

    @abstractmethod
    async def test(self) -> str: ...

    async def aclose(self) -> None:
        return None


KINDS: dict[str, type[BaseConnector]] = {}


def register[T: type[BaseConnector]](cls: T) -> T:
    KINDS[cls.kind] = cls
    return cls

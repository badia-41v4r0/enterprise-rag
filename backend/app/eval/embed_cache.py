"""Recorded real embeddings, so that the evaluation gate runs where no embedding model is available (CI).

These are outputs of the real `bge-m3` model, recorded with `erag eval embed-cache` on a machine with Ollama and
committed. They are looked up by the exact text the indexer or the question embeds; a text that is not in the
file is an error in strict mode (CI), never a guess. Values are stored as float16 and every vector handed out,
recorded or looked up, goes through the same rounding, so a live run and a cached run give identical numbers.

File format (little endian): b"ERAGEMB1", u16 model length, model (UTF-8), u32 dim, u32 count, then `count`
records sorted by key: 32-byte sha256(model + NUL + text) followed by `dim` float16 values.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Literal

from app.ingestion.embedder import Embedder

MAGIC = b"ERAGEMB1"


class EmbeddingCacheMiss(RuntimeError):
    pass


def text_key(model: str, text: str) -> bytes:
    return hashlib.sha256(model.encode() + b"\x00" + text.encode()).digest()


def _round(vec: list[float], dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}e", struct.pack(f"<{dim}e", *vec)))


class CachedEmbedder:
    """An `Embedder` backed by the recorded file; in `record` mode, misses go to the real embedder."""

    def __init__(
        self,
        path: Path,
        model: str,
        dim: int,
        mode: Literal["strict", "record"] = "strict",
        fallback: Embedder | None = None,
    ) -> None:
        if mode == "record" and fallback is None:
            raise ValueError("record mode needs the real embedder as fallback")
        self.path = path
        self.model = model
        self.dim = dim
        self.mode = mode
        self.fallback = fallback
        self.vectors: dict[bytes, tuple[float, ...]] = {}
        self.used: set[bytes] = set()
        self.recorded = 0
        if path.exists():
            self._load()

    def _load(self) -> None:
        raw = self.path.read_bytes()
        if not raw.startswith(MAGIC):
            raise ValueError(f"{self.path} is not an embedding cache")
        off = len(MAGIC)
        (mlen,) = struct.unpack_from("<H", raw, off)
        off += 2
        model = raw[off : off + mlen].decode()
        off += mlen
        dim, count = struct.unpack_from("<II", raw, off)
        off += 8
        if model != self.model or dim != self.dim:
            raise ValueError(f"{self.path} holds {model}/{dim}, expected {self.model}/{self.dim}")
        rec = 32 + 2 * dim
        fmt = f"<{dim}e"
        for i in range(count):
            start = off + i * rec
            self.vectors[raw[start : start + 32]] = struct.unpack_from(fmt, raw, start + 32)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        keys = [text_key(self.model, t) for t in texts]
        missing = [i for i, k in enumerate(keys) if k not in self.vectors]
        if missing:
            if self.mode == "strict" or self.fallback is None:
                raise EmbeddingCacheMiss(
                    f"{len(missing)} text(s) are not in the recorded embeddings ({self.path.name}); "
                    "run `erag eval embed-cache` on a machine with Ollama and commit the file"
                )
            fresh = await self.fallback.embed([texts[i] for i in missing])
            for i, vec in zip(missing, fresh, strict=True):
                self.vectors[keys[i]] = tuple(_round(vec, self.dim))
                self.recorded += 1
        self.used.update(keys)
        return [list(self.vectors[k]) for k in keys]

    def save(self, prune: bool = False) -> int:
        """Write the file (sorted, so it is reproducible). With `prune`, keep only what was used."""
        keep = sorted(k for k in self.vectors if not prune or k in self.used)
        model = self.model.encode()
        parts = [MAGIC, struct.pack("<H", len(model)), model, struct.pack("<II", self.dim, len(keep))]
        fmt = f"<{self.dim}e"
        for k in keep:
            parts.append(k)
            parts.append(struct.pack(fmt, *self.vectors[k]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"".join(parts))
        return len(keep)

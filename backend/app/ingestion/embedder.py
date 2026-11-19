"""Local embeddings via Ollama's OpenAI-compatible `/v1/embeddings` endpoint (always local).

Two embedders (R17, R18):
- `get_query_embedder()` for chat questions and evaluation: one short request, on `ERAG_QUERY_EMBED_URL` when
  set (a dedicated endpoint or a priority lane), so bulk ingestion never queues in front of a question.
- `get_ingest_embedder()` for the worker: batches spread round-robin over `ERAG_INGEST_EMBED_URLS` with
  `ERAG_INGEST_EMBED_CONCURRENCY` requests in flight; output order always matches input order.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Protocol

import httpx

from app.core.config import get_settings


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        *,
        base_urls: list[str] | None = None,
        concurrency: int = 1,
        batch_size: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        s = get_settings()
        urls = base_urls or [base_url or s.ollama_base_url]
        self.base_urls = [u.rstrip("/") for u in urls]
        self.base_url = self.base_urls[0]
        self.model = model or s.embed_model
        self.dim = dim or s.embed_dim
        self.concurrency = max(1, concurrency)
        self.batch_size = batch_size or s.embed_batch_size
        self.transport = transport

    async def embed(self, texts: list[str], batch_size: int | None = None) -> list[list[float]]:
        size = batch_size or self.batch_size
        batches = [texts[i : i + size] for i in range(0, len(texts), size)]
        results: list[list[list[float]]] = [[] for _ in batches]
        urls = itertools.cycle(self.base_urls)
        gate = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(timeout=120, transport=self.transport) as client:

            async def one(i: int, batch: list[str], url: str) -> None:
                async with gate:
                    results[i] = await self._post(client, url, batch)

            await asyncio.gather(*(one(i, b, next(urls)) for i, b in enumerate(batches)))
        return [vec for batch in results for vec in batch]

    async def _post(self, client: httpx.AsyncClient, url: str, batch: list[str]) -> list[list[float]]:
        try:
            r = await client.post(f"{url}/v1/embeddings", json={"model": self.model, "input": batch})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding request to Ollama failed: {exc}") from exc
        out = []
        for d in sorted(r.json()["data"], key=lambda d: d["index"]):
            vec = d["embedding"]
            if len(vec) != self.dim:
                raise EmbeddingError(f"Expected {self.dim}-dim embeddings from {self.model}, got {len(vec)}")
            out.append(vec)
        return out


def get_query_embedder() -> Embedder:
    s = get_settings()
    return OllamaEmbedder(s.query_embed_url or s.ollama_base_url)


def get_ingest_embedder() -> Embedder:
    s = get_settings()
    urls = [u.strip() for u in s.ingest_embed_urls.split(",") if u.strip()] or [s.ollama_base_url]
    return OllamaEmbedder(base_urls=urls, concurrency=s.ingest_embed_concurrency)


def get_embedder() -> Embedder:
    """Query embedder (the historical name, kept for callers that embed questions)."""
    return get_query_embedder()

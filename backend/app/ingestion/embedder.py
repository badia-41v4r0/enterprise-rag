"""Local embeddings via Ollama's OpenAI-compatible `/v1/embeddings` endpoint (always local)."""

from __future__ import annotations

from typing import Protocol

import httpx

from app.core.config import get_settings


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, base_url: str | None = None, model: str | None = None, dim: int | None = None) -> None:
        s = get_settings()
        self.base_url = (base_url or s.ollama_base_url).rstrip("/")
        self.model = model or s.embed_model
        self.dim = dim or s.embed_dim

    async def embed(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                try:
                    r = await client.post(f"{self.base_url}/v1/embeddings", json={"model": self.model, "input": batch})
                    r.raise_for_status()
                except httpx.HTTPError as exc:
                    raise EmbeddingError(f"Embedding request to Ollama failed: {exc}") from exc
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                for d in data:
                    vec = d["embedding"]
                    if len(vec) != self.dim:
                        raise EmbeddingError(f"Expected {self.dim}-dim embeddings from {self.model}, got {len(vec)}")
                    out.append(vec)
        return out


def get_embedder() -> Embedder:
    return OllamaEmbedder()

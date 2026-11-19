"""Reciprocal rank fusion of the retrieval legs (R14, ADR 0038)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.rag.vector_store import VectorHit


@dataclass
class Candidate:
    hit: VectorHit
    fused: float = 0.0
    vector_score: float | None = None  # cosine similarity, when the vector leg found it
    text_rank: float | None = None  # ts_rank_cd, when the full-text leg found it
    legs: list[str] = field(default_factory=list)


def rrf(
    vector_hits: list[VectorHit],
    text_hits: list[VectorHit],
    *,
    k: int = 60,
    vector_weight: float = 1.0,
    text_weight: float = 1.0,
) -> list[Candidate]:
    """score(chunk) = sum over legs of weight / (k + rank). No score normalization is needed, which matters
    because cosine similarity and ts_rank_cd live on different scales."""
    by_id: dict[uuid.UUID, Candidate] = {}
    for rank, h in enumerate(vector_hits, start=1):
        c = by_id.setdefault(h.chunk_id, Candidate(h))
        c.fused += vector_weight / (k + rank)
        c.vector_score = h.score
        c.legs.append("vector")
    for rank, h in enumerate(text_hits, start=1):
        c = by_id.setdefault(h.chunk_id, Candidate(h))
        c.fused += text_weight / (k + rank)
        c.text_rank = h.score
        c.legs.append("text")
    return sorted(by_id.values(), key=lambda c: (-c.fused, str(c.hit.chunk_id)))

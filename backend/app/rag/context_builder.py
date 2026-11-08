"""Build the model context from authorized hits: dedupe, token budget, citation numbers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models import Classification
from app.rag.vector_store import VectorHit


@dataclass
class ContextItem:
    n: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    container_name: str
    classification: Classification
    text: str
    meta: dict[str, Any]
    score: float

    def citation(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "title": self.title,
            "container": self.container_name,
            "classification": self.classification.value,
            "heading": self.meta.get("heading"),
            "page": self.meta.get("page"),
            "score": round(self.score, 4),
        }


@dataclass
class BuiltContext:
    items: list[ContextItem] = field(default_factory=list)
    token_count: int = 0
    skipped_for_budget: int = 0

    @property
    def classification(self) -> Classification:
        return Classification.max(*(i.classification for i in self.items))

    @property
    def chunk_ids(self) -> list[uuid.UUID]:
        return [i.chunk_id for i in self.items]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def build_context(hits: list[VectorHit], token_budget: int) -> BuiltContext:
    ctx = BuiltContext()
    seen_ids: set[uuid.UUID] = set()
    seen_text: set[str] = set()
    for h in sorted(hits, key=lambda h: h.score, reverse=True):
        key = _norm(h.text)
        if h.chunk_id in seen_ids or key in seen_text:
            continue
        if ctx.token_count + h.token_count > token_budget:
            ctx.skipped_for_budget += 1
            continue
        seen_ids.add(h.chunk_id)
        seen_text.add(key)
        ctx.token_count += h.token_count
        ctx.items.append(
            ContextItem(
                n=len(ctx.items) + 1,
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                title=h.title,
                container_name=h.container_name,
                classification=h.classification,
                text=h.text,
                meta=h.meta,
                score=h.score,
            )
        )
    return ctx

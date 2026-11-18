"""Authorized retrieval: filtered vector search, then an ACL re-check against Postgres."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, can_read_document
from app.ingestion.embedder import Embedder
from app.models import Document
from app.rag.fusion import rrf
from app.rag.reranker import RerankerUnavailable, rerank
from app.rag.settings import RetrievalSettings
from app.rag.settings import load as load_retrieval_settings
from app.rag.vector_store import AccessFilter, PgVectorStore, VectorHit, VectorStore
from app.services.audit import audit


@dataclass
class RetrievalResult:
    hits: list[VectorHit]
    dropped_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    mode: str = "vector"
    reranked: bool = False
    reranker_fallback: str | None = None  # why the reranker was not used although configured
    legs: dict[str, int] = field(default_factory=dict)


async def recheck(db: AsyncSession, perms: EffectivePermissions, hits: list[VectorHit]) -> RetrievalResult:
    """Defense in depth: verify every hit against the *current* documents table."""
    if not hits:
        return RetrievalResult([])
    rows = (
        await db.execute(
            select(Document.id, Document.container_id, Document.restricted, Document.current_version_id).where(
                Document.id.in_({h.document_id for h in hits})
            )
        )
    ).all()
    state = {r[0]: r for r in rows}
    kept: list[VectorHit] = []
    dropped: list[VectorHit] = []
    for h in hits:
        row = state.get(h.document_id)
        ok = (
            row is not None
            and row[1] == h.container_id
            and row[3] == h.version_id
            and can_read_document(perms, container_id=row[1], document_id=row[0], restricted=row[2])
        )
        (kept if ok else dropped).append(h)
    return RetrievalResult(kept, [h.chunk_id for h in dropped])


class Retriever:
    """The retrieval pipeline (R14):

    vector leg + full-text leg (same authorization clauses) -> reciprocal rank fusion -> ACL recheck ->
    reranker in process (or the no-reranker thresholds) -> top k.

    The recheck runs before the reranker reads any text. Runtime settings come from Admin > Retrieval unless
    the caller passes them (evaluation runs).
    """

    def __init__(self, db: AsyncSession, embedder: Embedder, store: VectorStore | None = None) -> None:
        self.db = db
        self.embedder = embedder
        self.store = store or PgVectorStore(db)

    async def retrieve_by_vector(
        self, perms: EffectivePermissions, vector: list[float], k: int, min_score: float = 0.0
    ) -> RetrievalResult:
        """Vector leg only, with a cosine floor (no fusion, no reranker)."""
        hits = await self.store.search(vector, AccessFilter.from_permissions(perms), k)
        hits = [h for h in hits if h.score >= min_score]
        return await self._recheck(perms, hits, legs={"vector": len(hits)})

    async def _recheck(self, perms: EffectivePermissions, hits: list[VectorHit], **kw: object) -> RetrievalResult:
        result = await recheck(self.db, perms, hits)
        if result.dropped_chunk_ids:
            audit(
                self.db,
                "retrieval.acl_recheck_mismatch",
                user_id=perms.user_id,
                decision="deny",
                details={"chunk_ids": [str(c) for c in result.dropped_chunk_ids]},
            )
        for name, value in kw.items():
            setattr(result, name, value)
        return result

    async def retrieve(
        self,
        perms: EffectivePermissions,
        query: str,
        k: int | None = None,
        min_score: float | None = None,
        *,
        settings: RetrievalSettings | None = None,
        vector: list[float] | None = None,
    ) -> RetrievalResult:
        if not perms.readable_containers:
            return RetrievalResult([])
        cfg = settings or await load_retrieval_settings(self.db)
        if k is not None or min_score is not None:
            cfg = dataclasses.replace(
                cfg,
                final_k=k if k is not None else cfg.final_k,
                vector_min_score=min_score if min_score is not None else cfg.vector_min_score,
            )
        if vector is None:
            vector = (await self.embedder.embed([query]))[0]
        access = AccessFilter.from_permissions(perms)
        use_reranker = bool(cfg.reranker)
        if cfg.mode == "vector" and not use_reranker:
            return await self.retrieve_by_vector(perms, vector, cfg.final_k, cfg.vector_min_score)

        vector_hits = await self.store.search(vector, access, cfg.vector_k)
        text_hits = await self.store.search_text(query, access, cfg.text_k) if cfg.mode == "hybrid" else []
        fused = rrf(vector_hits, text_hits, k=cfg.rrf_k, vector_weight=cfg.vector_weight, text_weight=cfg.text_weight)
        legs = {"vector": len(vector_hits), "text": len(text_hits)}
        checked = await self._recheck(perms, [c.hit for c in fused], mode=cfg.mode, legs=legs)
        kept = {h.chunk_id for h in checked.hits}
        candidates = [c for c in fused if c.hit.chunk_id in kept]

        fallback: str | None = None
        if use_reranker and candidates:
            pool = candidates[: cfg.rerank_candidates]
            try:
                scores = await rerank(cfg.reranker, query, [_rerank_text(c.hit) for c in pool])
            except RerankerUnavailable as exc:
                fallback = str(exc)
            else:
                for c, s in zip(pool, scores, strict=True):
                    c.hit.score = s
                ranked = sorted(pool, key=lambda c: -c.hit.score)
                checked.hits = [c.hit for c in ranked if c.hit.score >= cfg.rerank_min_score][: cfg.final_k]
                checked.reranked = True
                return checked
        # No reranker (or its fallback): fused order with the per-leg floors.
        out = []
        for c in candidates:
            vec_ok = c.vector_score is not None and c.vector_score >= cfg.vector_min_score
            text_ok = c.text_rank is not None and c.text_rank >= cfg.text_min_rank
            if vec_ok or text_ok:
                c.hit.score = c.vector_score if c.vector_score is not None else c.fused
                out.append(c.hit)
        checked.hits = out[: cfg.final_k]
        checked.reranker_fallback = fallback
        return checked


def _rerank_text(h: VectorHit) -> str:
    heading = h.meta.get("heading")
    return f"{h.title}\n{heading}\n{h.text}" if heading else f"{h.title}\n{h.text}"

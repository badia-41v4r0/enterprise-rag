"""Authorized retrieval: filtered vector search, then an ACL re-check against Postgres."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, can_read_document
from app.ingestion.embedder import Embedder
from app.models import Document
from app.rag.vector_store import AccessFilter, PgVectorStore, VectorHit, VectorStore
from app.services.audit import audit


@dataclass
class RetrievalResult:
    hits: list[VectorHit]
    dropped_chunk_ids: list[uuid.UUID] = field(default_factory=list)


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
    def __init__(self, db: AsyncSession, embedder: Embedder, store: VectorStore | None = None) -> None:
        self.db = db
        self.embedder = embedder
        self.store = store or PgVectorStore(db)

    async def retrieve_by_vector(
        self, perms: EffectivePermissions, vector: list[float], k: int, min_score: float = 0.0
    ) -> RetrievalResult:
        hits = await self.store.search(vector, AccessFilter.from_permissions(perms), k)
        hits = [h for h in hits if h.score >= min_score]
        result = await recheck(self.db, perms, hits)
        if result.dropped_chunk_ids:
            audit(
                self.db,
                "retrieval.acl_recheck_mismatch",
                user_id=perms.user_id,
                decision="deny",
                details={"chunk_ids": [str(c) for c in result.dropped_chunk_ids]},
            )
        return result

    async def retrieve(
        self, perms: EffectivePermissions, query: str, k: int, min_score: float = 0.0
    ) -> RetrievalResult:
        if not perms.readable_containers:
            return RetrievalResult([])
        vector = (await self.embedder.embed([query]))[0]
        return await self.retrieve_by_vector(perms, vector, k, min_score)

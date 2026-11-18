"""Vector storage behind an interface (ADR 0005). The access filter is mandatory (ADR 0002)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import ColumnElement, and_, delete, false, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions
from app.models import Chunk, Classification, Container, Document


@dataclass(frozen=True)
class AccessFilter:
    """What the user may read, resolved from Postgres for this request."""

    container_ids: frozenset[uuid.UUID]
    restricted_document_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @classmethod
    def from_permissions(cls, perms: EffectivePermissions) -> AccessFilter:
        return cls(perms.readable_containers, perms.readable_restricted_documents)


@dataclass
class VectorHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    container_id: uuid.UUID
    version_id: uuid.UUID
    ordinal: int
    text: str
    token_count: int
    meta: dict[str, Any]
    title: str
    container_name: str
    classification: Classification
    restricted: bool
    score: float


# HNSW candidate list size per query; part of the configuration snapshot of every evaluation run.
HNSW_EF_SEARCH = 100


# Ceiling on candidates fetched while compensating for documents rejected after the chunk-level filter.
MAX_CANDIDATES = 2000


def chunk_access_clause(access: AccessFilter) -> ColumnElement[bool]:
    """Authorization on columns of `chunks` only (ADR 0002: chunks carry location, never ACLs). Every
    retrieval leg starts from this clause; `document_access_clause` completes it."""
    return Chunk.container_id.in_(access.container_ids)


def document_access_clause(access: AccessFilter) -> ColumnElement[bool]:
    """The rest of the authorization, on the joined document: same container, current version only, and
    restricted documents only with an explicit grant."""
    restricted_ok = (
        or_(Document.restricted.is_(False), Document.id.in_(access.restricted_document_ids))
        if access.restricted_document_ids
        else or_(Document.restricted.is_(False), false())
    )
    return and_(
        Document.container_id.in_(access.container_ids),
        Chunk.container_id == Document.container_id,
        Chunk.version_id == Document.current_version_id,
        restricted_ok,
    )


class VectorStore(Protocol):
    async def upsert_chunks(self, chunks: list[Chunk]) -> None: ...
    async def delete_versions(self, version_ids: list[uuid.UUID]) -> None: ...
    async def search(self, query_vector: list[float], access: AccessFilter, k: int) -> list[VectorHit]: ...


class PgVectorStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert_chunks(self, chunks: list[Chunk]) -> None:
        self.db.add_all(chunks)
        await self.db.flush()

    async def delete_versions(self, version_ids: list[uuid.UUID]) -> None:
        if version_ids:
            await self.db.execute(delete(Chunk).where(Chunk.version_id.in_(version_ids)))

    async def search(self, query_vector: list[float], access: AccessFilter, k: int) -> list[VectorHit]:
        if not access.container_ids:
            return []
        # Keep scanning the HNSW index until enough rows pass the filter (pgvector >= 0.8).
        await self.db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        await self.db.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH:d}"))
        distance = Chunk.embedding.cosine_distance(query_vector)
        # The inner query filters on columns of `chunks` only, so an ordered HNSW scan can drive it (or, for a
        # user who reads little, the planner picks the container index and an exact sort). Documents are joined
        # afterwards; candidates they reject (restricted, not current) are compensated by over-fetching.
        limit = k + max(8, k // 2)
        while True:
            cand = (
                select(Chunk.id.label("cid"), distance.label("distance"))
                .where(chunk_access_clause(access))
                .order_by(distance)
                .limit(limit)
                .subquery()
            )
            q = (
                select(
                    Chunk,
                    Document.title,
                    Document.restricted,
                    Container.name,
                    Container.classification,
                    cand.c.distance,
                )
                .join(cand, cand.c.cid == Chunk.id)
                .join(Document, Document.id == Chunk.document_id)
                .join(Container, Container.id == Document.container_id)
                .where(document_access_clause(access))
                .order_by(cand.c.distance)
                .limit(k)
            )
            rows = (await self.db.execute(q)).all()
            if len(rows) >= k or limit >= MAX_CANDIDATES:
                break
            fetched = await self.db.scalar(select(func.count()).select_from(cand))
            if (fetched or 0) < limit:  # nothing more to scan
                break
            limit *= 4
        return [
            VectorHit(
                chunk_id=c.id,
                document_id=c.document_id,
                container_id=c.container_id,
                version_id=c.version_id,
                ordinal=c.ordinal,
                text=c.text,
                token_count=c.token_count,
                meta=c.meta,
                title=title,
                container_name=cname,
                classification=cls,
                restricted=restricted,
                score=1.0 - float(dist),
            )
            for c, title, restricted, cname, cls, dist in rows
        ]

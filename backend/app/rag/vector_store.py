"""Vector storage behind an interface (ADR 0005). The access filter is mandatory (ADR 0002)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, false, or_, select, text
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
        # Keep scanning the HNSW index until k rows pass the filter (pgvector >= 0.8).
        await self.db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        await self.db.execute(text("SET LOCAL hnsw.ef_search = 100"))
        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        restricted_ok = (
            or_(Document.restricted.is_(False), Document.id.in_(access.restricted_document_ids))
            if access.restricted_document_ids
            else or_(Document.restricted.is_(False), false())
        )
        q = (
            select(Chunk, Document.title, Document.restricted, Container.name, Container.classification, distance)
            .join(Document, Document.id == Chunk.document_id)
            .join(Container, Container.id == Document.container_id)
            # --- authorization filter, applied inside the vector query ---
            .where(Document.container_id.in_(access.container_ids))
            .where(Chunk.container_id == Document.container_id)
            .where(restricted_ok)
            # --- only the current version of each document ---
            .where(Chunk.version_id == Document.current_version_id)
            .order_by(distance)
            .limit(k)
        )
        rows = (await self.db.execute(q)).all()
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

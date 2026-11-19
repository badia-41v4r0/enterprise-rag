"""Vector storage behind an interface (ADR 0005). The access filter is mandatory (ADR 0002)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import (
    ColumnElement,
    Text,
    and_,
    any_,
    bindparam,
    cast,
    delete,
    false,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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

# Below this many readable chunks, and this share of the corpus, an exact scan over the user's containers is
# cheaper than walking the HNSW graph looking for rare allowed rows (PLAN-v0.5 3.1, docs/benchmarks/r16-*.md).
EXACT_SCAN_MAX_ROWS = 20_000
EXACT_SCAN_MAX_SHARE = 0.10

# container set -> (estimated readable chunks, total chunks). Planner statistics drift slowly.
_ESTIMATES: dict[frozenset[uuid.UUID], tuple[int, int]] = {}
_ESTIMATES_MAX = 5_000


def _any_of(values: frozenset[uuid.UUID]) -> ColumnElement[Any]:
    """`= ANY(:ids)` with one array parameter instead of one bind parameter per id: a user with thousands of
    containers or restricted-document grants sends a single value, and the plan does not grow with the list.
    The ids still come from app.authz.policy, the single place where permissions are decided."""
    return any_(bindparam("acl_ids", sorted(values), type_=ARRAY(PG_UUID(as_uuid=True)), unique=True))


def chunk_access_clause(access: AccessFilter) -> ColumnElement[bool]:
    """Authorization on columns of `chunks` only (ADR 0002: chunks carry location, never ACLs). Every
    retrieval leg starts from this clause; `document_access_clause` completes it."""
    return Chunk.container_id == _any_of(access.container_ids)


def document_access_clause(access: AccessFilter) -> ColumnElement[bool]:
    """The rest of the authorization, on the joined document: same container, current version only, and
    restricted documents only with an explicit grant."""
    restricted_ok = (
        or_(Document.restricted.is_(False), Document.id == _any_of(access.restricted_document_ids))
        if access.restricted_document_ids
        else or_(Document.restricted.is_(False), false())
    )
    return and_(
        Document.container_id == _any_of(access.container_ids),
        Chunk.container_id == Document.container_id,
        Chunk.version_id == Document.current_version_id,
        restricted_ok,
    )


class VectorStore(Protocol):
    async def upsert_chunks(self, chunks: list[Chunk]) -> None: ...
    async def delete_versions(self, version_ids: list[uuid.UUID]) -> None: ...
    async def search(self, query_vector: list[float], access: AccessFilter, k: int) -> list[VectorHit]: ...
    async def search_text(self, question: str, access: AccessFilter, k: int) -> list[VectorHit]: ...


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
        exact = await self.prefers_exact_scan(access)
        if exact:
            # Few readable chunks: bitmap scan on the container index plus an exact sort (also exact recall).
            # Switched back right after, so the rest of the transaction plans normally.
            await self.db.execute(text("SET LOCAL enable_indexscan = off"))
        else:
            # Keep scanning the HNSW index until enough rows pass the filter (pgvector >= 0.8).
            await self.db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
            await self.db.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH:d}"))
        try:
            rows = await self._search_rows(query_vector, access, k)
        finally:
            if exact:
                await self.db.execute(text("SET LOCAL enable_indexscan = on"))
        return [
            _hit(c, title, restricted, cname, cls, 1.0 - float(dist)) for c, title, restricted, cname, cls, dist in rows
        ]

    async def prefers_exact_scan(self, access: AccessFilter) -> bool:
        """From the planner's estimate of the user's readable chunks (statistics, no counting), cached per
        container set. Only the plan changes; the filter is the same either way."""
        hit = _ESTIMATES.get(access.container_ids)
        if hit is None:

            async def estimate(sql: str, params: dict[str, Any]) -> int:
                plan = (await self.db.execute(text("EXPLAIN (FORMAT JSON) " + sql), params)).scalar_one()
                return int(plan[0]["Plan"]["Plan Rows"])

            # Both from the planner, so they stay consistent even before the first ANALYZE.
            readable = await estimate(
                "SELECT 1 FROM chunks WHERE container_id = ANY(:ids)", {"ids": sorted(access.container_ids)}
            )
            hit = (readable, max(await estimate("SELECT 1 FROM chunks", {}), 1))
            if len(_ESTIMATES) >= _ESTIMATES_MAX:
                _ESTIMATES.clear()
            _ESTIMATES[access.container_ids] = hit
        rows, total = hit
        return rows <= EXACT_SCAN_MAX_ROWS and rows / total <= EXACT_SCAN_MAX_SHARE

    async def _search_rows(self, query_vector: list[float], access: AccessFilter, k: int) -> list[Any]:
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
        return list(rows)

    async def text_query(self, question: str) -> str | None:
        """The OR tsquery of a question (see app.rag.lexical), or None when it has no usable term."""
        from app.rag.lexical import QUERY_SQL

        value = await self.db.scalar(text(f"SELECT ({QUERY_SQL})::text"), {"q": question})
        return value or None

    async def search_text(self, question: str, access: AccessFilter, k: int) -> list[VectorHit]:
        """Full-text leg: the same authorization clauses as `search`, ranked by ts_rank_cd."""
        if not access.container_ids:
            return []
        tsq_text = await self.text_query(question)
        if tsq_text is None:
            return []
        tsq = func.to_tsquery(literal_column("'simple'::regconfig"), cast(tsq_text, Text))
        rank = func.ts_rank_cd(Chunk.tsv, tsq, 33)  # 1|32: length-normalized, scaled to [0, 1)
        limit = k + max(8, k // 2)
        while True:
            cand = (
                select(Chunk.id.label("cid"), rank.label("rank"))
                .where(chunk_access_clause(access), Chunk.tsv.op("@@")(tsq))
                .order_by(rank.desc())
                .limit(limit)
                .subquery()
            )
            q = (
                select(
                    Chunk, Document.title, Document.restricted, Container.name, Container.classification, cand.c.rank
                )
                .join(cand, cand.c.cid == Chunk.id)
                .join(Document, Document.id == Chunk.document_id)
                .join(Container, Container.id == Document.container_id)
                .where(document_access_clause(access))
                .order_by(cand.c.rank.desc(), Chunk.id)
                .limit(k)
            )
            rows = (await self.db.execute(q)).all()
            if len(rows) >= k or limit >= MAX_CANDIDATES:
                break
            fetched = await self.db.scalar(select(func.count()).select_from(cand))
            if (fetched or 0) < limit:
                break
            limit *= 4
        return [_hit(c, title, restricted, cname, cls, float(r)) for c, title, restricted, cname, cls, r in rows]


def _hit(c: Chunk, title: str, restricted: bool, cname: str, cls: Classification, score: float) -> VectorHit:
    return VectorHit(
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
        score=score,
    )

"""Turn a stored document version into embedded chunks and make it the current version."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.blobstore import BlobStore
from app.ingestion.chunker import chunk_sections
from app.ingestion.embedder import Embedder
from app.ingestion.parsers import parse
from app.models import Chunk, Document, DocumentVersion, VersionStatus
from app.rag.vector_store import PgVectorStore

log = logging.getLogger(__name__)


class IngestionError(ValueError):
    """Permanent failure (not retried)."""

    pass


def embedding_input(title: str, meta: dict[str, object], text: str) -> str:
    heading = meta.get("heading")
    return f"{title}\n{heading}\n{text}" if heading else f"{title}\n{text}"


async def index_version(db: AsyncSession, version_id: uuid.UUID, blobs: BlobStore, embedder: Embedder) -> int:
    """Parse, chunk, embed and insert. Swaps the document's current version atomically.

    Only this document is touched: chunks of older versions are deleted and those versions
    are marked superseded in the same transaction. Returns the number of chunks created.
    """
    version = await db.get(DocumentVersion, version_id)
    if version is None:
        raise IngestionError(f"version {version_id} not found")
    doc = await db.get(Document, version.document_id)
    if doc is None:
        raise IngestionError(f"document {version.document_id} not found")

    data = await blobs.get(version.blob_key)
    sections = parse(data, doc.filename)
    drafts = chunk_sections(sections)
    if not drafts:
        raise IngestionError("no extractable text")
    vectors = await embedder.embed([embedding_input(doc.title, d.meta, d.text) for d in drafts])

    store = PgVectorStore(db)
    old_versions = list(
        (
            await db.scalars(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id == doc.id, DocumentVersion.id != version.id
                )
            )
        ).all()
    )
    await store.delete_versions([version.id, *old_versions])  # idempotent on retries
    await store.upsert_chunks(
        [
            Chunk(
                version_id=version.id,
                document_id=doc.id,
                container_id=doc.container_id,
                ordinal=d.ordinal,
                text=d.text,
                token_count=d.token_count,
                embedding=vec,
                meta=d.meta,
            )
            for d, vec in zip(drafts, vectors, strict=True)
        ]
    )
    if old_versions:
        await db.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id.in_(old_versions), DocumentVersion.status == VersionStatus.ready)
            .values(status=VersionStatus.superseded)
        )
    version.status = VersionStatus.ready
    version.error = None
    doc.current_version_id = version.id
    log.info("indexed version", extra={"version_id": str(version.id), "chunks": len(drafts)})
    return len(drafts)

"""Re-index current document versions in the background (`erag reindex`, R14).

Each document is re-processed by the worker through the normal path (`index_version`), which replaces the
version's chunks inside one transaction: readers see the old chunks until the new ones are committed, so the
re-index needs no downtime. Only current versions are queued; superseded versions have no chunks.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.chunker import CHUNKER_VERSION
from app.models import Chunk, Container, Document, IngestionJob, JobState
from app.services.audit import audit


async def queue_reindex(db: AsyncSession, *, container_slug: str | None = None, stale_only: bool = True) -> int:
    q = select(Document.current_version_id).where(Document.current_version_id.is_not(None))
    if container_slug:
        q = q.join(Container, Container.id == Document.container_id).where(Container.slug == container_slug)
    if stale_only:
        q = q.where(
            exists().where(Chunk.version_id == Document.current_version_id, Chunk.chunker_version < CHUNKER_VERSION)
        )
    pending = select(IngestionJob.version_id).where(IngestionJob.state.in_([JobState.queued, JobState.running]))
    q = q.where(Document.current_version_id.not_in(pending))
    n = 0
    for (version_id,) in (await db.execute(q)).all():
        db.add(IngestionJob(version_id=version_id))
        n += 1
    audit(
        db,
        "ingestion.reindex_queued",
        details={"versions": n, "container": container_slug, "stale_only": stale_only, "chunker": CHUNKER_VERSION},
    )
    await db.commit()
    return n

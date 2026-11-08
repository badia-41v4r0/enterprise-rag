"""Ingestion worker: claims jobs from `ingestion_jobs` with FOR UPDATE SKIP LOCKED (no Redis)."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.blobstore import BlobStore, get_blobstore
from app.ingestion.embedder import Embedder, get_embedder
from app.ingestion.indexer import index_version
from app.models import DocumentVersion, IngestionJob, JobState, VersionStatus
from app.services.audit import audit

log = logging.getLogger(__name__)


async def claim_job(db: AsyncSession) -> IngestionJob | None:
    job = await db.scalar(
        select(IngestionJob)
        .where(IngestionJob.state == JobState.queued)
        .order_by(IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.state = JobState.running
    job.attempts += 1
    return job


async def process_one(
    sessionmaker: async_sessionmaker[AsyncSession], blobs: BlobStore, embedder: Embedder
) -> uuid.UUID | None:
    """Claim and process one job. Returns the job id, or None when the queue is empty."""
    async with sessionmaker() as db:
        job = await claim_job(db)
        if job is None:
            return None
        job_id, version_id = job.id, job.version_id
        version = await db.get(DocumentVersion, version_id)
        if version is not None:
            version.status = VersionStatus.processing
        await db.commit()

    max_attempts = get_settings().job_max_attempts
    async with sessionmaker() as db:
        try:
            n = await index_version(db, version_id, blobs, embedder)
            job = await db.get(IngestionJob, job_id)
            assert job is not None
            job.state = JobState.done
            job.error = None
            audit(db, "ingestion.indexed", resource=f"version:{version_id}", details={"chunks": n})
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.exception("ingestion failed", extra={"job_id": str(job_id)})
            job = await db.get(IngestionJob, job_id)
            version = await db.get(DocumentVersion, version_id)
            assert job is not None
            job.error = str(exc)[:2000]
            retry = job.attempts < max_attempts and not isinstance(exc, ValueError)
            job.state = JobState.queued if retry else JobState.failed
            if version is not None and not retry:
                version.status = VersionStatus.failed
                version.error = job.error
            audit(
                db, "ingestion.failed", resource=f"version:{version_id}", decision="error", details={"error": job.error}
            )
            await db.commit()
    return job_id


async def drain(blobs: BlobStore | None = None, embedder: Embedder | None = None) -> int:
    """Process jobs until the queue is empty. Returns the number processed."""
    sm = get_sessionmaker()
    blobs = blobs or get_blobstore()
    embedder = embedder or get_embedder()
    n = 0
    while await process_one(sm, blobs, embedder) is not None:
        n += 1
    return n


async def run_worker(once: bool = False) -> None:
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log.info("worker started")
    from app.connectors.sync import process_next, schedule_due

    last_schedule = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            n = await drain()
            if n:
                log.info("processed jobs", extra={"count": n})
            if loop.time() - last_schedule >= settings.connector_scheduler_seconds:
                last_schedule = loop.time()
                async with get_sessionmaker()() as db:
                    await schedule_due(db)
            # One connector sync per iteration, then its documents are ingested on the next pass.
            if await process_next(get_sessionmaker()) is not None:
                continue
        except Exception:
            log.exception("worker loop error")
        if once:
            return
        await asyncio.sleep(settings.worker_poll_seconds)

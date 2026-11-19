"""Ingestion worker: claims jobs from `ingestion_jobs` with FOR UPDATE SKIP LOCKED (no Redis).

At scale (R18, PLAN-v0.5 5.2):
- several workers, each running `worker_concurrency` jobs at once;
- priorities: an upload someone is waiting for before a connector sync, a re-index last;
- failures retry with exponential backoff (`next_attempt_at`); when retries run out the job becomes a dead
  letter (`dead`), shown with a Retry button; a permanent error (an unparseable file) fails at once;
- a running job holds `locked_until` by heartbeat; the reaper puts jobs (and sync runs) whose worker died back
  in the queue;
- on SIGTERM a worker finishes the jobs it holds and stops (graceful shutdown).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.metrics import INGEST_JOBS, INGEST_SECONDS, REAPED
from app.core.metrics import start as start_metrics
from app.ingestion.blobstore import BlobStore, get_blobstore
from app.ingestion.embedder import Embedder, get_ingest_embedder
from app.ingestion.indexer import index_version
from app.models import DocumentVersion, IngestionJob, JobState, SyncRun, VersionStatus
from app.services.audit import audit

log = logging.getLogger(__name__)

PRIORITY_INTERACTIVE = 10  # an upload in the UI
PRIORITY_CONNECTOR = 0
PRIORITY_REINDEX = -10
MAX_BACKOFF = timedelta(hours=1)


def _lease() -> timedelta:
    return timedelta(seconds=get_settings().job_lease_seconds)


def backoff(attempts: int) -> timedelta:
    """Delay before the next attempt after `attempts` failures: base x 2^(attempts-1), capped at an hour."""
    base = get_settings().job_retry_base_seconds
    return min(timedelta(seconds=base * 2 ** max(attempts - 1, 0)), MAX_BACKOFF)


async def claim_job(db: AsyncSession, now: datetime | None = None) -> IngestionJob | None:
    now = now or datetime.now(UTC)
    job = await db.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.state == JobState.queued,
            or_(IngestionJob.next_attempt_at.is_(None), IngestionJob.next_attempt_at <= now),
        )
        .order_by(IngestionJob.priority.desc(), IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.state = JobState.running
    job.attempts += 1
    job.locked_until = now + _lease()
    return job


async def _heartbeat(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> None:
    interval = max(get_settings().job_lease_seconds / 3, 1.0)
    while True:
        await asyncio.sleep(interval)
        async with sessionmaker() as db:
            await db.execute(
                update(IngestionJob)
                .where(IngestionJob.id == job_id, IngestionJob.state == JobState.running)
                .values(locked_until=datetime.now(UTC) + _lease())
            )
            await db.commit()


async def reap(db: AsyncSession, now: datetime | None = None) -> int:
    """Return jobs and sync runs whose worker stopped heart-beating to the queue. Idempotent; any worker may run
    it. A reaped job keeps its attempt count, so a job that kills its worker every time ends as a dead letter."""
    now = now or datetime.now(UTC)
    jobs = await db.execute(
        update(IngestionJob)
        .where(IngestionJob.state == JobState.running, IngestionJob.locked_until < now)
        .values(state=JobState.queued, locked_until=None, error="worker stopped; re-queued")
    )
    runs = await db.execute(
        update(SyncRun).where(SyncRun.status == "running", SyncRun.locked_until < now).values(status="queued")
    )
    await db.commit()
    n = int(jobs.rowcount) + int(runs.rowcount)  # type: ignore[attr-defined]
    if n:
        REAPED.inc(n)
        log.warning("reaper re-queued %d stale jobs or sync runs", n)
    return n


async def process_one(
    sessionmaker: async_sessionmaker[AsyncSession], blobs: BlobStore, embedder: Embedder
) -> uuid.UUID | None:
    """Claim and process one job. Returns the job id, or None when nothing is ready."""
    async with sessionmaker() as db:
        job = await claim_job(db)
        if job is None:
            return None
        job_id, version_id = job.id, job.version_id
        version = await db.get(DocumentVersion, version_id)
        if version is not None:
            version.status = VersionStatus.processing
        await db.commit()

    s = get_settings()
    beat = asyncio.create_task(_heartbeat(sessionmaker, job_id))
    try:
        async with sessionmaker() as db:
            try:
                started = time.perf_counter()
                n = await index_version(db, version_id, blobs, embedder)
                job = await db.get(IngestionJob, job_id)
                assert job is not None
                job.state = JobState.done
                INGEST_JOBS.inc(outcome="done")
                INGEST_SECONDS.observe(time.perf_counter() - started)
                job.error = None
                job.locked_until = None
                audit(db, "ingestion.indexed", resource=f"version:{version_id}", details={"chunks": n})
                await db.commit()
            except Exception as exc:
                await db.rollback()
                log.exception("ingestion failed", extra={"job_id": str(job_id)})
                job = await db.get(IngestionJob, job_id)
                version = await db.get(DocumentVersion, version_id)
                assert job is not None
                job.error = str(exc)[:2000]
                job.locked_until = None
                permanent = isinstance(exc, ValueError)
                if not permanent and job.attempts < s.job_max_attempts:
                    job.state = JobState.queued
                    job.next_attempt_at = datetime.now(UTC) + backoff(job.attempts)
                    INGEST_JOBS.inc(outcome="retry")
                else:
                    job.state = JobState.failed if permanent else JobState.dead
                    INGEST_JOBS.inc(outcome=job.state.value)
                    if version is not None:
                        version.status = VersionStatus.failed
                        version.error = job.error
                audit(
                    db,
                    "ingestion.failed",
                    resource=f"version:{version_id}",
                    decision="error",
                    details={"error": job.error, "state": job.state.value, "attempts": job.attempts},
                )
                await db.commit()
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    return job_id


async def drain(
    blobs: BlobStore | None = None, embedder: Embedder | None = None, concurrency: int | None = None
) -> int:
    """Process jobs until none is ready, `concurrency` at a time. Returns the number processed."""
    sm = get_sessionmaker()
    blobs = blobs or get_blobstore()
    embedder = embedder or get_ingest_embedder()
    lanes = max(1, concurrency or get_settings().worker_concurrency)
    counts = [0] * lanes

    async def lane(i: int) -> None:
        while not _stopping.is_set() and await process_one(sm, blobs, embedder) is not None:
            counts[i] += 1

    await asyncio.gather(*(lane(i) for i in range(lanes)))
    return sum(counts)


_stopping = asyncio.Event()


def request_stop(*_: object) -> None:
    """SIGTERM/SIGINT: take no new work; the jobs in hand finish (graceful shutdown)."""
    log.info("worker stopping: finishing the jobs in hand")
    _stopping.set()


async def run_worker(once: bool = False) -> None:
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    loop = asyncio.get_running_loop()
    if not once:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, request_stop)
    log.info("worker started", extra={"concurrency": settings.worker_concurrency})
    metrics_server = await start_metrics(
        settings.metrics_host, settings.metrics_port + 1 if settings.metrics_port else 0
    )
    from app.connectors.sync import process_next, schedule_due

    last_schedule = 0.0
    while not _stopping.is_set():
        try:
            async with get_sessionmaker()() as db:
                await reap(db)
            n = await drain()
            if n:
                log.info("processed jobs", extra={"count": n})
            if loop.time() - last_schedule >= settings.connector_scheduler_seconds:
                last_schedule = loop.time()
                async with get_sessionmaker()() as db:
                    await schedule_due(db)
            # One connector sync per iteration, then its documents are ingested on the next pass.
            if not _stopping.is_set() and await process_next(get_sessionmaker()) is not None:
                continue
        except Exception:
            log.exception("worker loop error")
        if once:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_stopping.wait(), settings.worker_poll_seconds)
    if metrics_server is not None:
        metrics_server.close()
    log.info("worker stopped")

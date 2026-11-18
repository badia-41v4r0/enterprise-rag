"""Ingestion at scale (R18): priorities, retries with backoff, dead letters, the reaper, back-pressure, the
memory cap and parallel jobs. The embedder is a deterministic stand-in: these tests are about the queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.blobstore import get_blobstore
from app.ingestion.embedder import EmbeddingError
from app.ingestion.worker import backoff, claim_job, drain, reap
from app.models import EMBED_DIM, Classification, DocumentVersion, IngestionJob, JobState, SyncRun, VersionStatus
from app.services.documents import upload_document
from tests.factories import login, make_container, make_user

pytestmark = pytest.mark.db


class FakeEmbedder:
    model = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingError("embedding endpoint down")
        return [[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts]


async def upload(db: AsyncSession, name: str, *, user_id=None, body: bytes | None = None):  # type: ignore[no-untyped-def]
    c = await make_container(db, f"C-{name}", Classification.internal)
    return await upload_document(
        db,
        get_blobstore(),
        container=c,
        filename=f"{name}.md",
        data=body or f"# {name}\n\nSome text about {name}.".encode(),
        user_id=user_id,
    )


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    s = get_settings()

    def set_(**values: object) -> None:
        for k, v in values.items():
            monkeypatch.setattr(s, k, v)

    return set_


async def test_uploads_go_before_connector_syncs_and_reindex_goes_last(db: AsyncSession) -> None:
    u = await make_user(db, "u@x.test")
    sync = await upload(db, "from-connector")  # no user: a connector
    interactive = await upload(db, "from-ui", user_id=u.id)
    reindexed = await upload(db, "reindex")
    await db.execute(update(IngestionJob).where(IngestionJob.version_id == reindexed.version_id).values(priority=-10))
    await db.commit()
    order = []
    for _ in range(3):
        async with get_sessionmaker()() as s:
            job = await claim_job(s)
            assert job is not None
            order.append(job.version_id)
            await s.commit()
    assert order == [interactive.version_id, sync.version_id, reindexed.version_id]


async def test_transient_failures_back_off_then_become_a_dead_letter(db: AsyncSession, settings_override) -> None:  # type: ignore[no-untyped-def]
    settings_override(job_max_attempts=2, job_retry_base_seconds=60.0, worker_concurrency=1)
    up = await upload(db, "flaky")
    down = FakeEmbedder(fail=True)

    assert await drain(embedder=down) == 1
    job = await db.scalar(select(IngestionJob).where(IngestionJob.version_id == up.version_id))
    assert job is not None and job.state == JobState.queued and job.attempts == 1
    job_id = job.id
    assert job.next_attempt_at is not None and job.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)
    assert await drain(embedder=down) == 0  # not before its time

    await db.execute(update(IngestionJob).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    assert await drain(embedder=down) == 1
    db.expire_all()
    job = await db.get(IngestionJob, job_id)
    version = await db.get(DocumentVersion, up.version_id)
    assert job is not None and job.state == JobState.dead and job.attempts == 2
    assert version is not None and version.status == VersionStatus.failed


async def test_backoff_doubles_and_is_capped(settings_override) -> None:  # type: ignore[no-untyped-def]
    settings_override(job_retry_base_seconds=30.0)
    assert [backoff(n).total_seconds() for n in (1, 2, 3)] == [30.0, 60.0, 120.0]
    assert backoff(30) == timedelta(hours=1)


async def test_dead_letter_retry_from_the_admin_screen(
    db: AsyncSession, client: httpx.AsyncClient, settings_override
) -> None:  # type: ignore[no-untyped-def]
    settings_override(job_max_attempts=1)
    await make_user(db, "admin@x.test", admin=True)
    up = await upload(db, "doomed")
    assert await drain(embedder=FakeEmbedder(fail=True)) == 1
    job = await db.scalar(select(IngestionJob).where(IngestionJob.version_id == up.version_id))
    assert job is not None and job.state == JobState.dead
    job_id = job.id
    await login(client, "admin@x.test")
    r = await client.post(f"/api/admin/jobs/{job_id}/retry")
    assert r.status_code == 200 and r.json()["state"] == "queued" and r.json()["attempts"] == 0
    assert (await client.post(f"/api/admin/jobs/{job_id}/retry")).status_code == 409  # not dead any more
    assert await drain(embedder=FakeEmbedder()) == 1
    db.expire_all()
    job = await db.get(IngestionJob, job_id)
    version = await db.get(DocumentVersion, up.version_id)
    assert job is not None and job.state == JobState.done
    assert version is not None and version.status == VersionStatus.ready


async def test_reaper_requeues_work_whose_worker_died(db: AsyncSession) -> None:
    up = await upload(db, "orphan")
    async with get_sessionmaker()() as s:
        job = await claim_job(s)  # a worker claims it, then dies without a heartbeat
        assert job is not None
        await s.commit()
    from app.models import Connector

    c = await make_container(db, "Conn", Classification.internal)
    conn = Connector(container_id=c.id, kind="folder", name="f", config={"path": "/nonexistent"})
    db.add(conn)
    await db.flush()
    db.add(SyncRun(connector_id=conn.id, status="running", locked_until=datetime.now(UTC) - timedelta(seconds=5)))
    await db.commit()

    assert await reap(db) == 1  # only the run: the job's lease is still valid
    await db.execute(update(IngestionJob).values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    assert await reap(db) == 1  # now the job
    db.expire_all()
    job = await db.scalar(select(IngestionJob).where(IngestionJob.version_id == up.version_id))
    run = await db.scalar(select(SyncRun))
    assert job is not None and job.state == JobState.queued and job.attempts == 1  # the attempt still counts
    assert run is not None and run.status == "queued"


async def test_back_pressure_stops_the_scheduler(db: AsyncSession, settings_override) -> None:  # type: ignore[no-untyped-def]
    from app.connectors.sync import queue_full, schedule_due
    from app.models import Connector

    settings_override(ingest_queue_max=2)
    c = await make_container(db, "Conn", Classification.internal)
    db.add(Connector(container_id=c.id, kind="folder", name="f", config={"path": "/x"}, schedule_minutes=5))
    await db.commit()
    await upload(db, "a")
    assert not await queue_full(db, get_settings())
    await upload(db, "b")
    assert await queue_full(db, get_settings())
    assert await schedule_due(db) == 0  # full: no new syncs this round
    assert await db.scalar(select(SyncRun.id)) is None


async def test_memory_cap_refuses_large_files_without_retrying(db: AsyncSession, settings_override) -> None:  # type: ignore[no-untyped-def]
    settings_override(ingest_max_blob_mb=1)
    up = await upload(db, "huge", body=b"# big\n\n" + b"x" * (1024 * 1024 + 10))
    emb = FakeEmbedder()
    assert await drain(embedder=emb) == 1
    db.expire_all()
    job = await db.scalar(select(IngestionJob).where(IngestionJob.version_id == up.version_id))
    assert job is not None and job.state == JobState.failed and "ingestion limit" in (job.error or "")
    assert emb.calls == 0


async def test_parallel_lanes_process_every_job_once(db: AsyncSession) -> None:
    ups = [await upload(db, f"doc{i}") for i in range(6)]
    emb = FakeEmbedder()
    assert await drain(embedder=emb, concurrency=3) == 6
    db.expire_all()
    states = (await db.scalars(select(IngestionJob.state))).all()
    assert sorted(s.value for s in states) == ["done"] * 6
    assert {v.status for v in (await db.scalars(select(DocumentVersion))).all()} == {VersionStatus.ready}
    assert len(ups) == 6

"""Sync engine: upsert by external id and content hash through the normal versioning path.

- Unchanged items (same version hint, or same SHA-256) are skipped.
- Changed items become a new version of the same document (only that document is re-indexed).
- Items gone from the source are removed from the index, but only after a COMPLETE listing.
- Each item is isolated: one failure is recorded and the sync continues.
- Documents land in the connector's container and inherit its permissions (no per-file ACLs).
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.connectors.folder
import app.connectors.web
from app.connectors.base import KINDS, BaseConnector, ConnectorError, ItemSkipped
from app.core.config import Settings, get_settings
from app.core.netguard import BlockedDestination
from app.ingestion.blobstore import BlobStore, get_blobstore
from app.models import Connector, ConnectorItem, Container, Document, SyncRun
from app.services import vault
from app.services.audit import audit
from app.services.documents import upload_document

log = logging.getLogger(__name__)
MAX_ERRORS_KEPT = 200

_KINDS_LOADED = (app.connectors.folder, app.connectors.web)


@dataclass
class SyncReport:
    counts: Counter[str] = field(default_factory=Counter)
    errors: list[dict[str, str]] = field(default_factory=list)
    preview: list[dict[str, str]] = field(default_factory=list)
    complete: bool = True
    fatal: str | None = None

    def error(self, item: str, message: str) -> None:
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append({"item": item[:500], "error": message[:500]})


def validate_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    cls = KINDS.get(kind)
    if cls is None:
        raise ConnectorError(f"unknown connector kind {kind!r}")
    return cls.Config.model_validate(config).model_dump()


def config_secret_names(kind: str, config: dict[str, Any]) -> list[str]:
    cls = KINDS[kind]
    cfg = cls.Config.model_validate(config)
    return [str(getattr(cfg, f)) for f in cls.secret_fields if getattr(cfg, f, None)]


async def instantiate(db: AsyncSession, connector: Connector, settings: Settings | None = None) -> BaseConnector:
    """Build the connector with exactly the secrets assigned to it (through the vault, audited)."""
    s = settings or get_settings()
    cls = KINDS.get(connector.kind)
    if cls is None:
        raise ConnectorError(f"unknown connector kind {connector.kind!r}")
    secrets = {
        name: await vault.reveal_assigned(
            db, name, consumer=f"connector:{connector.id}", assigned=connector.secret_names
        )
        for name in config_secret_names(connector.kind, connector.config)
    }
    return cls(connector.config, secrets, s)


def _title(name: str) -> str:
    return PurePosixPath(name).stem.replace("_", " ").replace("-", " ")[:500] or name[:500]


async def _document_for(
    db: AsyncSession, connector: Connector, container: Container, prev: ConnectorItem | None, name: str
) -> Document:
    doc = await db.get(Document, prev.document_id) if prev is not None and prev.document_id else None
    if doc is not None:
        return doc
    filename = f"{connector.name}/{name}"[:500]
    doc = await db.scalar(select(Document).where(Document.container_id == container.id, Document.filename == filename))
    if doc is None:
        doc = Document(container_id=container.id, filename=filename, title=_title(name), restricted=False)
        db.add(doc)
        await db.flush()
    return doc


async def run_sync(
    db: AsyncSession,
    connector: Connector,
    *,
    dry_run: bool = False,
    blobs: BlobStore | None = None,
    settings: Settings | None = None,
) -> SyncReport:
    s = settings or get_settings()
    blobs = blobs or get_blobstore()
    report = SyncReport()
    container = await db.get(Container, connector.container_id)
    if container is None:
        report.fatal = "container no longer exists"
        return report
    known = {
        i.external_id: i
        for i in (await db.scalars(select(ConnectorItem).where(ConnectorItem.connector_id == connector.id))).all()
    }
    configured_max = int(connector.config.get("max_items", s.connector_max_items))
    limit = min(configured_max, s.connector_max_items)
    max_bytes = s.connector_max_item_mb * 1024 * 1024
    seen: set[str] = set()
    try:
        impl = await instantiate(db, connector, s)
    except (ConnectorError, vault.VaultError, ValueError) as exc:
        report.fatal = str(exc)
        return report
    now = datetime.now(UTC)
    try:
        async for item in impl.list_items():
            if len(seen) >= limit:
                report.error(item.external_id, f"item limit ({limit}) reached; the rest was not synced")
                report.complete = False
                break
            seen.add(item.external_id)
            prev = known.get(item.external_id)
            try:
                if prev and prev.document_id and item.version_hint and prev.version_hint == item.version_hint:
                    prev.last_seen_at = now
                    report.counts["unchanged"] += 1
                    continue
                if dry_run:
                    action = "update" if prev and prev.document_id else "create"
                    report.counts[action] += 1
                    report.preview.append({"item": item.external_id, "action": action})
                    continue
                fetched = await impl.fetch(item, prev.version_hint if prev else None)
                if len(fetched.data) > max_bytes:
                    raise ItemSkipped(f"larger than {s.connector_max_item_mb} MB")
                digest = hashlib.sha256(fetched.data).hexdigest()
                if prev and prev.document_id and (fetched.unchanged or prev.content_hash == digest):
                    prev.version_hint, prev.last_seen_at = fetched.version_hint, now
                    report.counts["unchanged"] += 1
                    continue
                doc = await _document_for(db, connector, container, prev, item.name)
                is_new = doc.current_version_id is None and not (prev and prev.document_id)
                await upload_document(
                    db,
                    blobs,
                    container=container,
                    filename=doc.filename,
                    data=fetched.data,
                    user_id=None,
                    document=doc,
                    dedupe=False,
                    audit_details={"connector_id": str(connector.id), "external_id": item.external_id[:500]},
                )
                if prev is None:
                    prev = ConnectorItem(connector_id=connector.id, external_id=item.external_id)
                    db.add(prev)
                    known[item.external_id] = prev
                prev.document_id, prev.content_hash = doc.id, digest
                prev.version_hint, prev.last_seen_at = fetched.version_hint, now
                await db.commit()
                report.counts["created" if is_new else "updated"] += 1
            except ItemSkipped as exc:
                report.counts["skipped"] += 1
                report.error(item.external_id, str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the sync
                await db.rollback()
                log.warning("connector item failed", extra={"item": item.external_id, "error": str(exc)})
                report.counts["failed"] += 1
                report.error(item.external_id, f"{type(exc).__name__}: {exc}")
                connector = await db.get(Connector, connector.id) or connector
                known = {
                    i.external_id: i
                    for i in (
                        await db.scalars(select(ConnectorItem).where(ConnectorItem.connector_id == connector.id))
                    ).all()
                }
    except (ConnectorError, BlockedDestination, httpx.HTTPError, OSError) as exc:
        report.fatal = str(exc)
        report.complete = False
    finally:
        await impl.aclose()
    for item_id, reason in impl.skipped:
        report.counts["skipped"] += 1
        report.error(item_id, reason)

    if report.complete and report.fatal is None:
        for ext_id, it in known.items():
            if ext_id in seen:
                continue
            report.counts["deleted"] += 1
            if dry_run:
                report.preview.append({"item": ext_id, "action": "delete"})
                continue
            if it.document_id is not None:
                gone = await db.get(Document, it.document_id)
                if gone is not None:
                    gone.current_version_id = None
                    await db.flush()
                    await db.delete(gone)
            await db.delete(it)
        if not dry_run:
            await db.commit()
    report.counts["seen"] = len(seen)
    return report


# --- Jobs ---------------------------------------------------------------------------------------


async def enqueue(db: AsyncSession, connector: Connector, triggered_by: str, dry_run: bool = False) -> SyncRun:
    run = SyncRun(connector_id=connector.id, status="queued", dry_run=dry_run, triggered_by=triggered_by[:100])
    db.add(run)
    connector.status = "queued"
    await db.flush()
    return run


async def execute_run(db: AsyncSession, run: SyncRun, settings: Settings | None = None) -> SyncRun:
    connector = await db.get(Connector, run.connector_id)
    started = time.monotonic()
    run.status, run.started_at = "running", datetime.now(UTC)
    if connector is not None:
        connector.status = "running"
    await db.commit()
    if connector is None:
        run.status = "failed"
        run.errors = {"items": [], "fatal": "connector deleted"}
        await db.commit()
        return run
    connector_id, dry = connector.id, run.dry_run
    report = await run_sync(db, connector, dry_run=dry, settings=settings)
    run = await db.get(SyncRun, run.id) or run
    connector = await db.get(Connector, connector_id) or connector
    run.status = "failed" if report.fatal else "done"
    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.monotonic() - started) * 1000)
    run.counts = dict(report.counts)
    run.errors = {"items": report.errors, "fatal": report.fatal, "preview": report.preview[:500]}
    if not dry:
        connector.status = "error" if report.fatal else "ok"
        connector.last_error = report.fatal
        connector.last_sync_at = run.finished_at
    else:
        connector.status = "ok" if connector.last_sync_at else "idle"
    audit(
        db,
        "connector.sync",
        resource=f"connector:{connector_id}",
        decision="error" if report.fatal else "allow",
        details={"run_id": str(run.id), "dry_run": dry, "counts": run.counts, "fatal": report.fatal},
    )
    await db.commit()
    return run


async def claim_run(db: AsyncSession) -> SyncRun | None:
    return await db.scalar(
        select(SyncRun)
        .where(SyncRun.status == "queued")
        .order_by(SyncRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


async def process_next(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    async with sessionmaker() as db:
        run = await claim_run(db)
        if run is None:
            return None
        run.status = "running"
        await db.commit()
        run_id = run.id
    async with sessionmaker() as db:
        run = await db.get(SyncRun, run_id)
        if run is not None:
            try:
                await execute_run(db, run)
            except Exception as exc:
                log.exception("sync run crashed")
                await db.rollback()
                failed = await db.get(SyncRun, run_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.errors = {"items": [], "fatal": f"{type(exc).__name__}: {exc}"}
                    await db.commit()
    return run_id


async def schedule_due(db: AsyncSession) -> int:
    """Queue a sync for every enabled connector whose interval elapsed and that has no run pending."""
    now = datetime.now(UTC)
    n = 0
    for c in (
        await db.scalars(select(Connector).where(Connector.enabled.is_(True), Connector.schedule_minutes.is_not(None)))
    ).all():
        assert c.schedule_minutes is not None
        if c.last_sync_at is not None and c.last_sync_at > now - timedelta(minutes=c.schedule_minutes):
            continue
        pending = await db.scalar(
            select(SyncRun.id).where(SyncRun.connector_id == c.id, SyncRun.status.in_(["queued", "running"]))
        )
        if pending is None:
            await enqueue(db, c, "schedule")
            n += 1
    await db.commit()
    return n

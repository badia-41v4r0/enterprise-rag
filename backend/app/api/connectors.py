"""Connectors (admin): create, edit, test, dry-run preview, sync now, run history (ADR 0020)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_admin, require_step_up
from app.connectors.base import KINDS, ConnectorError
from app.connectors.folder import resolve_folder
from app.connectors.sync import config_secret_names, enqueue, instantiate, run_sync, validate_config
from app.core.config import get_settings
from app.core.db import get_db
from app.core.netguard import BlockedDestination
from app.models import Connector, ConnectorItem, Container, SyncRun
from app.services import vault
from app.services.audit import audit

router = APIRouter(prefix="/api/admin", tags=["connectors"])

WARNING = (
    "Everyone with read access to the target container will see everything this connector imports. "
    "Permissions of the source system are not copied: point it only at content whose audience matches the container."
)


class ConnectorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    container_id: uuid.UUID
    config: dict[str, Any] = Field(default_factory=dict)
    secret_names: list[str] = Field(default_factory=list)
    schedule_minutes: int | None = Field(default=None, ge=5, le=10_080)
    enabled: bool = True


class ConnectorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None
    secret_names: list[str] | None = None
    schedule_minutes: int | None = Field(default=None, ge=0, le=10_080)  # 0 = manual only
    enabled: bool | None = None


class ConnectorOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    container_id: uuid.UUID
    container_name: str
    config: dict[str, Any]
    secret_names: list[str]
    schedule_minutes: int | None
    enabled: bool
    status: str
    last_sync_at: datetime | None
    last_error: str | None
    item_count: int


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    dry_run: bool
    triggered_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    counts: dict[str, Any]
    errors: dict[str, Any]


class PreviewOut(BaseModel):
    counts: dict[str, int]
    items: list[dict[str, str]]
    errors: list[dict[str, str]]
    fatal: str | None
    warning: str = WARNING


@vault.register_reference_finder
async def _connector_refs(db: AsyncSession) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in (await db.scalars(select(Connector))).all():
        for n in c.secret_names:
            out.setdefault(n, []).append(f"connector: {c.name}")
    return out


async def _out(db: AsyncSession, c: Connector) -> ConnectorOut:
    container = await db.get(Container, c.container_id)
    items = await db.scalar(select(func.count()).select_from(ConnectorItem).where(ConnectorItem.connector_id == c.id))
    return ConnectorOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        container_id=c.container_id,
        container_name=container.name if container else "?",
        config=c.config,
        secret_names=c.secret_names,
        schedule_minutes=c.schedule_minutes,
        enabled=c.enabled,
        status=c.status,
        last_sync_at=c.last_sync_at,
        last_error=c.last_error,
        item_count=items or 0,
    )


async def _validate(db: AsyncSession, kind: str, config: dict[str, Any], secret_names: list[str]) -> dict[str, Any]:
    try:
        clean = validate_config(kind, config)
        if kind == "folder":
            resolve_folder(str(clean["path"]), list(get_settings().connector_roots))
        needed = config_secret_names(kind, clean)
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)) from exc
    for n in set(needed) | set(secret_names):
        if not await vault.exists(db, n):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"secret {n!r} does not exist")
    missing = set(needed) - set(secret_names)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"assign the secrets the config uses: {', '.join(sorted(missing))}"
        )
    return clean


async def _connector_or_404(db: AsyncSession, connector_id: uuid.UUID) -> Connector:
    c = await db.get(Connector, connector_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector not found")
    return c


@router.get("/connector-kinds")
async def connector_kinds(_: AuthContext = Depends(require_admin)) -> dict[str, Any]:
    return {
        "kinds": {k: cls.Config.model_json_schema() for k, cls in KINDS.items()},
        "roots": [str(r) for r in get_settings().connector_roots],
        "warning": WARNING,
    }


@router.get("/connectors")
async def list_connectors(
    _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[ConnectorOut]:
    return [await _out(db, c) for c in (await db.scalars(select(Connector).order_by(Connector.name))).all()]


@router.post("/connectors", status_code=201)
async def create_connector(
    body: ConnectorIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> ConnectorOut:
    if await db.get(Container, body.container_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Container not found")
    config = await _validate(db, body.kind, body.config, body.secret_names)
    c = Connector(
        name=body.name,
        kind=body.kind,
        container_id=body.container_id,
        config=config,
        secret_refs={"names": sorted(set(body.secret_names))},
        schedule_minutes=body.schedule_minutes,
        enabled=body.enabled,
        created_by=auth.user.id,
    )
    db.add(c)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connector name already exists") from exc
    audit(
        db,
        "admin.connector.create",
        user_id=auth.user.id,
        resource=f"connector:{c.id}",
        details={"name": c.name, "kind": c.kind, "container_id": str(c.container_id), "config": config},
    )
    await db.commit()
    return await _out(db, c)


@router.patch("/connectors/{connector_id}")
async def update_connector(
    connector_id: uuid.UUID,
    body: ConnectorPatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    c = await _connector_or_404(db, connector_id)
    names = body.secret_names if body.secret_names is not None else c.secret_names
    config = await _validate(db, c.kind, body.config if body.config is not None else c.config, names)
    c.config = config
    c.secret_refs = {"names": sorted(set(names))}
    if body.name is not None:
        c.name = body.name
    if body.schedule_minutes is not None:
        c.schedule_minutes = body.schedule_minutes or None
    if body.enabled is not None:
        c.enabled = body.enabled
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connector name already exists") from exc
    audit(
        db,
        "admin.connector.update",
        user_id=auth.user.id,
        resource=f"connector:{c.id}",
        details=body.model_dump(exclude_none=True),
    )
    await db.commit()
    return await _out(db, c)


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: uuid.UUID,
    delete_documents: bool = Query(True),
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a connector and (by default) the documents it imported."""
    from app.models import Document

    c = await _connector_or_404(db, connector_id)
    removed = 0
    if delete_documents:
        doc_ids = (
            await db.scalars(
                select(ConnectorItem.document_id).where(
                    ConnectorItem.connector_id == c.id, ConnectorItem.document_id.is_not(None)
                )
            )
        ).all()
        for did in doc_ids:
            d = await db.get(Document, did)
            if d is not None:
                d.current_version_id = None
                await db.flush()
                await db.delete(d)
                removed += 1
    await db.delete(c)
    audit(
        db,
        "admin.connector.delete",
        user_id=auth.user.id,
        resource=f"connector:{connector_id}",
        details={"name": c.name, "documents_deleted": removed},
    )
    await db.commit()


@router.post("/connectors/{connector_id}/test")
async def test_connector(
    connector_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    c = await _connector_or_404(db, connector_id)
    try:
        impl = await instantiate(db, c)
        try:
            message = await impl.test()
        finally:
            await impl.aclose()
        ok = True
    except (ConnectorError, BlockedDestination, vault.VaultError, OSError, ValueError) as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # noqa: BLE001 - report network errors to the admin
        ok, message = False, f"{type(exc).__name__}: {exc}"
    audit(db, "admin.connector.test", user_id=auth.user.id, resource=f"connector:{c.id}", details={"ok": ok})
    await db.commit()
    return {"ok": ok, "message": message}


@router.post("/connectors/{connector_id}/preview")
async def preview_connector(
    connector_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> PreviewOut:
    """Dry run: what a sync would create, update or delete. Nothing is imported."""
    c = await _connector_or_404(db, connector_id)
    report = await run_sync(db, c, dry_run=True)
    audit(db, "admin.connector.preview", user_id=auth.user.id, resource=f"connector:{c.id}")
    await db.commit()
    return PreviewOut(counts=dict(report.counts), items=report.preview[:500], errors=report.errors, fatal=report.fatal)


@router.post("/connectors/{connector_id}/sync", status_code=202)
async def sync_connector(
    connector_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> RunOut:
    """Queue a sync; the worker runs it and the new documents go through normal ingestion."""
    c = await _connector_or_404(db, connector_id)
    pending = await db.scalar(
        select(SyncRun).where(SyncRun.connector_id == c.id, SyncRun.status.in_(["queued", "running"]))
    )
    run = pending or await enqueue(db, c, f"user:{auth.user.id}")
    audit(db, "admin.connector.sync", user_id=auth.user.id, resource=f"connector:{c.id}", details={"run": str(run.id)})
    await db.commit()
    return _run_out(run)


def _run_out(r: SyncRun) -> RunOut:
    return RunOut(
        id=r.id,
        status=r.status,
        dry_run=r.dry_run,
        triggered_by=r.triggered_by,
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
        duration_ms=r.duration_ms,
        counts=r.counts or {},
        errors=r.errors or {},
    )


@router.get("/connectors/{connector_id}/runs")
async def list_runs(
    connector_id: uuid.UUID, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[RunOut]:
    rows = (
        await db.scalars(
            select(SyncRun).where(SyncRun.connector_id == connector_id).order_by(SyncRun.created_at.desc()).limit(50)
        )
    ).all()
    return [_run_out(r) for r in rows]

"""Containers and documents visible to the current user; uploads for managers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth, require_admin
from app.authz.policy import can, can_read_document
from app.core.config import get_settings
from app.core.db import get_db
from app.ingestion.blobstore import get_blobstore
from app.ingestion.parsers import UnsupportedFormat
from app.models import (
    Action,
    Chunk,
    Classification,
    Container,
    Document,
    DocumentVersion,
    IngestionJob,
    ResourceType,
    VersionStatus,
)
from app.services.audit import audit
from app.services.documents import upload_document

router = APIRouter(prefix="/api", tags=["documents"])


class ContainerView(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    classification: Classification
    can_read: bool
    can_manage: bool


class VersionView(BaseModel):
    id: uuid.UUID
    sha256: str
    status: VersionStatus
    error: str | None
    size_bytes: int
    created_at: datetime


class DocumentView(BaseModel):
    id: uuid.UUID
    container_id: uuid.UUID
    title: str
    filename: str
    restricted: bool
    current_version_id: uuid.UUID | None
    latest_status: VersionStatus | None
    chunk_count: int


class DocumentDetail(DocumentView):
    versions: list[VersionView]


class UploadOut(BaseModel):
    status: str
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    sha256: str


class DocumentPatch(BaseModel):
    title: str | None = None
    restricted: bool | None = None


class ChunkView(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    container_name: str
    ordinal: int
    text: str
    meta: dict[str, Any]


class JobView(BaseModel):
    id: uuid.UUID
    version_id: uuid.UUID
    state: str
    attempts: int
    error: str | None
    created_at: datetime


@router.get("/containers")
async def my_containers(
    auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> list[ContainerView]:
    """Containers the user can read or manage (admins see all, for management)."""
    p = auth.perms
    visible = p.readable_containers | p.manageable_containers
    q = select(Container).order_by(Container.name)
    if not p.is_admin:
        if not visible:
            return []
        q = q.where(Container.id.in_(visible))
    return [
        ContainerView(
            id=c.id,
            name=c.name,
            slug=c.slug,
            description=c.description,
            classification=c.classification,
            can_read=c.id in p.readable_containers,
            can_manage=can(p, Action.manage, ResourceType.container, c.id),
        )
        for c in (await db.scalars(q)).all()
    ]


async def _container_or_404(db: AsyncSession, auth: AuthContext, container_id: uuid.UUID, action: Action) -> Container:
    c = await db.get(Container, container_id)
    # Same 404 for "missing" and "not allowed": do not reveal existence.
    if c is None or not (
        can(auth.perms, action, ResourceType.container, c.id)
        or (action == Action.read and can(auth.perms, Action.manage, ResourceType.container, c.id))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Container not found")
    return c


async def _doc_views(db: AsyncSession, docs: list[Document]) -> list[DocumentView]:
    if not docs:
        return []
    ids = [d.id for d in docs]
    counts = dict(
        (
            await db.execute(
                select(Chunk.document_id, func.count())
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.document_id.in_(ids), Chunk.version_id == Document.current_version_id)
                .group_by(Chunk.document_id)
            )
        ).all()
    )
    latest: dict[uuid.UUID, VersionStatus] = {}
    for doc_id, st in (
        await db.execute(
            select(DocumentVersion.document_id, DocumentVersion.status)
            .where(DocumentVersion.document_id.in_(ids))
            .order_by(DocumentVersion.created_at)
        )
    ).all():
        latest[doc_id] = st
    return [
        DocumentView(
            id=d.id,
            container_id=d.container_id,
            title=d.title,
            filename=d.filename,
            restricted=d.restricted,
            current_version_id=d.current_version_id,
            latest_status=latest.get(d.id),
            chunk_count=counts.get(d.id, 0),
        )
        for d in docs
    ]


@router.get("/containers/{container_id}/documents")
async def list_documents(
    container_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> list[DocumentView]:
    c = await _container_or_404(db, auth, container_id, Action.read)
    docs = list(
        (await db.scalars(select(Document).where(Document.container_id == c.id).order_by(Document.title))).all()
    )
    if not can(auth.perms, Action.manage, ResourceType.container, c.id):
        docs = [
            d
            for d in docs
            if can_read_document(auth.perms, container_id=c.id, document_id=d.id, restricted=d.restricted)
        ]
    return await _doc_views(db, docs)


@router.post("/containers/{container_id}/documents", status_code=201)
async def upload(
    container_id: uuid.UUID,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    restricted: bool | None = Form(None),
    auth: AuthContext = Depends(get_auth),
    db: AsyncSession = Depends(get_db),
) -> UploadOut:
    c = await _container_or_404(db, auth, container_id, Action.manage)
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
    try:
        res = await upload_document(
            db,
            get_blobstore(),
            container=c,
            filename=file.filename or "upload.txt",
            data=data,
            user_id=auth.user.id,
            title=title,
            restricted=restricted,
        )
    except (UnsupportedFormat, ValueError) as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    return UploadOut(status=res.status, document_id=res.document_id, version_id=res.version_id, sha256=res.sha256)


async def _document_or_404(db: AsyncSession, auth: AuthContext, document_id: uuid.UUID, action: Action) -> Document:
    d = await db.get(Document, document_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    ok = can(auth.perms, action, ResourceType.document, d.id, container_id=d.container_id, restricted=d.restricted) or (
        action == Action.read and can(auth.perms, Action.manage, ResourceType.container, d.container_id)
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return d


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> DocumentDetail:
    d = await _document_or_404(db, auth, document_id, Action.read)
    view = (await _doc_views(db, [d]))[0]
    versions = (
        await db.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == d.id).order_by(DocumentVersion.created_at)
        )
    ).all()
    return DocumentDetail(
        **view.model_dump(),
        versions=[
            VersionView(
                id=v.id,
                sha256=v.sha256,
                status=v.status,
                error=v.error,
                size_bytes=v.size_bytes,
                created_at=v.created_at,
            )
            for v in versions
        ],
    )


@router.patch("/documents/{document_id}")
async def patch_document(
    document_id: uuid.UUID,
    body: DocumentPatch,
    auth: AuthContext = Depends(get_auth),
    db: AsyncSession = Depends(get_db),
) -> DocumentView:
    d = await _document_or_404(db, auth, document_id, Action.manage)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(d, k, v)
    audit(db, "document.update", user_id=auth.user.id, resource=f"document:{d.id}", details=changes)
    await db.commit()
    return (await _doc_views(db, [d]))[0]


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> None:
    d = await _document_or_404(db, auth, document_id, Action.manage)
    d.current_version_id = None
    await db.flush()
    await db.delete(d)
    audit(db, "document.delete", user_id=auth.user.id, resource=f"document:{document_id}")
    await db.commit()


@router.get("/chunks/{chunk_id}")
async def get_chunk(
    chunk_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> ChunkView:
    """Citation lookup. Requires read access to the document (manage is not enough)."""
    row = (
        await db.execute(
            select(Chunk, Document, Container.name)
            .join(Document, Document.id == Chunk.document_id)
            .join(Container, Container.id == Document.container_id)
            .where(Chunk.id == chunk_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    chunk, doc, cname = row
    if not can_read_document(auth.perms, container_id=doc.container_id, document_id=doc.id, restricted=doc.restricted):
        audit(db, "chunk.read", user_id=auth.user.id, resource=f"chunk:{chunk_id}", decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return ChunkView(
        id=chunk.id,
        document_id=doc.id,
        document_title=doc.title,
        container_name=cname,
        ordinal=chunk.ordinal,
        text=chunk.text,
        meta=chunk.meta,
    )


@router.get("/admin/jobs")
async def list_jobs(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[JobView]:
    jobs = (await db.scalars(select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(200))).all()
    return [
        JobView(
            id=j.id,
            version_id=j.version_id,
            state=j.state.value,
            attempts=j.attempts,
            error=j.error,
            created_at=j.created_at,
        )
        for j in jobs
    ]

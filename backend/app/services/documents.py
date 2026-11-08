"""Upload, dedupe and versioning of documents."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_hex
from app.ingestion.blobstore import BlobStore
from app.ingestion.parsers import mime_for
from app.models import Container, Document, DocumentVersion, IngestionJob, VersionStatus
from app.services.audit import audit


@dataclass
class UploadResult:
    status: str  # "queued" | "duplicate"
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    sha256: str


def safe_filename(name: str) -> str:
    base = PurePath(name.replace("\\", "/")).name.strip()
    if not base or base in {".", ".."}:
        raise ValueError("invalid filename")
    return base[:500]


async def upload_document(
    db: AsyncSession,
    blobs: BlobStore,
    *,
    container: Container,
    filename: str,
    data: bytes,
    user_id: uuid.UUID | None,
    title: str | None = None,
    restricted: bool | None = None,
    can_see: Callable[[uuid.UUID, bool], bool] | None = None,
    document: Document | None = None,
    dedupe: bool = True,
    audit_details: dict[str, str] | None = None,
) -> UploadResult:
    """Store a file and queue it for ingestion.

    - Same filename in the same container (or an explicit `document`) => new version of that document.
    - Identical content (sha256) to a live version in the same container => skipped, but only when
      `can_see(document_id, restricted)` allows it: deduplicating against a restricted document the
      uploader cannot read would confirm its content (a read oracle), so such matches are ignored.
    """
    filename = document.filename if document is not None else safe_filename(filename)
    mime = mime_for(filename)  # raises UnsupportedFormat early
    digest = sha256_hex(data)

    rows = (
        await db.execute(
            select(DocumentVersion.document_id, Document.restricted)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.container_id == container.id,
                DocumentVersion.sha256 == digest,
                DocumentVersion.status.in_([VersionStatus.pending, VersionStatus.processing, VersionStatus.ready]),
            )
        )
    ).all()
    dup = next((d for d, r in rows if can_see is None or can_see(d, r)), None) if dedupe else None
    if dup is not None:
        audit(
            db,
            "document.upload",
            user_id=user_id,
            resource=f"document:{dup}",
            decision="skip",
            details={"reason": "duplicate", "sha256": digest, "filename": filename},
        )
        await db.commit()
        return UploadResult("duplicate", dup, None, digest)

    doc = document or await db.scalar(
        select(Document).where(Document.container_id == container.id, Document.filename == filename)
    )
    if doc is None:
        doc = Document(
            container_id=container.id,
            filename=filename,
            title=title or PurePath(filename).stem.replace("_", " ").replace("-", " "),
            restricted=bool(restricted),
        )
        db.add(doc)
        await db.flush()
    else:
        if title:
            doc.title = title
        if restricted is not None:
            doc.restricted = restricted

    await blobs.put(digest, data)
    version = DocumentVersion(
        document_id=doc.id,
        sha256=digest,
        blob_key=digest,
        mime_type=mime,
        size_bytes=len(data),
        created_by=user_id,
    )
    db.add(version)
    await db.flush()
    db.add(IngestionJob(version_id=version.id))
    audit(
        db,
        "document.upload",
        user_id=user_id,
        resource=f"document:{doc.id}",
        details={
            "version_id": str(version.id),
            "sha256": digest,
            "filename": filename,
            "bytes": len(data),
            **(audit_details or {}),
        },
    )
    await db.commit()
    return UploadResult("queued", doc.id, version.id, digest)


async def requeue_version(db: AsyncSession, version: DocumentVersion, user_id: uuid.UUID | None) -> None:
    """Retry a failed version: back to pending with a fresh ingestion job. The caller commits."""
    version.status = VersionStatus.pending
    version.error = None
    db.add(IngestionJob(version_id=version.id))
    audit(
        db,
        "document.retry",
        user_id=user_id,
        resource=f"document:{version.document_id}",
        details={"version_id": str(version.id)},
    )

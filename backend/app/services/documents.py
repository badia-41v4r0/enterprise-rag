"""Upload, dedupe and versioning of documents."""

from __future__ import annotations

import uuid
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
) -> UploadResult:
    """Store a file and queue it for ingestion.

    - Same filename in the same container => new version of that document.
    - Identical content (sha256) to a live version in the same container => skipped.
    """
    filename = safe_filename(filename)
    mime = mime_for(filename)  # raises UnsupportedFormat early
    digest = sha256_hex(data)

    dup = await db.scalar(
        select(DocumentVersion.document_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.container_id == container.id,
            DocumentVersion.sha256 == digest,
            DocumentVersion.status.in_([VersionStatus.pending, VersionStatus.processing, VersionStatus.ready]),
        )
        .limit(1)
    )
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

    doc = await db.scalar(select(Document).where(Document.container_id == container.id, Document.filename == filename))
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
        details={"version_id": str(version.id), "sha256": digest, "filename": filename, "bytes": len(data)},
    )
    await db.commit()
    return UploadResult("queued", doc.id, version.id, digest)

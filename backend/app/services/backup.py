"""`erag backup verify` (R19, PLAN-v0.5 5.4): check that an installation (typically a freshly restored one) is
whole and usable, without printing or auditing any secret or content.

1. Schema: the database is at the migration head of this release.
2. Master key: every stored secret decrypts with the configured key (values are discarded at once). This is
   the check that the key backup, kept apart from the database backup, belongs with it.
3. Blobs: current document versions are present in the blob store and match their SHA-256 (a sample, or all
   with `full`).
4. Embeddings: every ready current version has chunks with embeddings (regenerating 20 million of them takes
   days, so the backup includes them; this confirms the restore did too).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Sealed, get_cipher
from app.ingestion.blobstore import BlobStore
from app.models import Chunk, Document, DocumentVersion, Secret, VersionStatus

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]}


def migration_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


async def verify(db: AsyncSession, blobs: BlobStore, *, sample: int = 50, full: bool = False) -> VerifyReport:
    report = VerifyReport()

    current = await db.scalar(text("SELECT version_num FROM alembic_version"))
    head = migration_head()
    report.add("schema", current == head, f"database at {current}, release head {head}")

    cipher = get_cipher()
    secrets_total = bad = 0
    for row in (await db.scalars(select(Secret))).all():
        secrets_total += 1
        try:
            cipher.open(Sealed(row.nonce, row.ciphertext, row.key_id), f"secret:{row.name}")
        except Exception:  # noqa: BLE001 - any failure means the key does not open this backup
            bad += 1
    report.add(
        "master_key",
        bad == 0,
        f"{secrets_total - bad}/{secrets_total} secrets decrypt" if secrets_total else "no secrets stored",
    )

    rows = (
        await db.execute(
            select(DocumentVersion.id, DocumentVersion.blob_key).join(
                Document, Document.current_version_id == DocumentVersion.id
            )
        )
    ).all()
    if sample <= 0 and not full:
        report.add("blobs", True, f"skipped (sample 0): {len(rows)} current versions not checked")
    else:
        chosen = rows if full or len(rows) <= sample else random.sample(rows, sample)
        missing = corrupt = 0
        for _vid, key in chosen:
            if not await blobs.exists(key):
                missing += 1
                continue
            if hashlib.sha256(await blobs.get(key)).hexdigest() != key:
                corrupt += 1
        report.add(
            "blobs",
            missing == 0 and corrupt == 0,
            f"checked {len(chosen)} of {len(rows)} current versions: {missing} missing, {corrupt} corrupt",
        )

    counts = (
        await db.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE NOT EXISTS "
                "(SELECT 1 FROM chunks c WHERE c.version_id = v.id AND c.embedding IS NOT NULL)) "
                "FROM document_versions v JOIN documents d ON d.current_version_id = v.id "
                "WHERE v.status = :ready"
            ),
            {"ready": VersionStatus.ready.value},
        )
    ).one()
    total_ready, without_chunks = int(counts[0]), int(counts[1])
    chunks = await db.scalar(select(func.count()).select_from(Chunk))
    report.add(
        "embeddings",
        not without_chunks,
        f"{total_ready} ready versions, {chunks} chunks, {without_chunks or 0} ready versions without chunks",
    )
    return report

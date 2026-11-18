"""The evaluation database: switching to it safely, creating and migrating it, and loading the corpus.

The evaluation corpus creates personas and synthetic confidential documents. It must never land in a real
installation, so every command here refuses to run when the evaluation (or benchmark) database is the
application's database.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import REPO_ROOT, get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.eval.corpus import DOC_DIRS, EVAL_CONTAINERS, EVAL_DIR, EVAL_PERSONAS, EVAL_ROLE_DOCUMENTS, EVAL_ROLES
from app.eval.embed_cache import CachedEmbedder
from app.ingestion.embedder import OllamaEmbedder

Mode = Literal["strict", "record"]


class EvalDatabaseError(RuntimeError):
    pass


def _identity(url: str) -> tuple[str | None, int | None, str | None]:
    u = make_url(url)
    return (u.host or "localhost", u.port or 5432, u.database)


def switch_database(kind: Literal["eval", "bench"] = "eval") -> str:
    """Point this process at the evaluation (or benchmark) database. Call before anything opens the engine."""
    s = get_settings()
    target = s.eval_db_url if kind == "eval" else s.bench_db_url
    if _identity(target) == _identity(s.database_url):
        raise EvalDatabaseError(
            f"the {kind} database is the application database ({_identity(target)[2]}); "
            f"set ERAG_{kind.upper()}_DATABASE_URL to a separate database"
        )
    os.environ["ERAG_DATABASE_URL"] = target
    get_settings.cache_clear()
    return target


async def ensure_database(url: str) -> None:
    """Create the database if missing (needs CREATEDB on the server) and run the migrations."""
    u = make_url(url)
    admin = create_async_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        await admin.dispose()
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        env={**os.environ, "ERAG_DATABASE_URL": url},
        capture_output=True,
    )


def cache_path() -> Path:
    return EVAL_DIR / "cache" / f"{get_settings().embed_model}.bin"


def build_embedder(mode: Mode = "strict") -> CachedEmbedder:
    s = get_settings()
    fallback = OllamaEmbedder() if mode == "record" else None
    return CachedEmbedder(cache_path(), s.embed_model, s.embed_dim, mode=mode, fallback=fallback)


async def _truncate_all() -> None:
    async with get_sessionmaker()() as db:
        names = (
            await db.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
            )
        ).all()
        await db.execute(text("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE"))
        await db.commit()


async def load_corpus(embedder: CachedEmbedder) -> dict[str, int]:
    """Empty the evaluation database, seed the structure and documents, and index them synchronously."""
    from app.ingestion.blobstore import LocalBlobStore
    from app.ingestion.worker import drain
    from app.models import DocumentVersion, IngestionJob, JobState
    from demo.acme.seed import seed_documents, seed_structure

    await _truncate_all()
    # Blobs are only needed while indexing: keep them in a temporary directory, never in the app's blob store.
    previous = os.environ.get("ERAG_BLOB_DIR")
    with tempfile.TemporaryDirectory(prefix="erag-eval-blobs-") as blob_dir:
        os.environ["ERAG_BLOB_DIR"] = blob_dir
        get_settings.cache_clear()
        try:
            async with get_sessionmaker()() as db:
                # Nobody logs in to the evaluation database: the personas get a random, discarded password.
                containers = await seed_structure(
                    db, secrets.token_urlsafe(24), specs=EVAL_CONTAINERS, role_grants=EVAL_ROLES, personas=EVAL_PERSONAS
                )
                queued = await seed_documents(
                    db, containers, specs=EVAL_CONTAINERS, doc_dirs=DOC_DIRS, role_documents=EVAL_ROLE_DOCUMENTS
                )
            indexed = await drain(blobs=LocalBlobStore(Path(blob_dir)), embedder=embedder)
        finally:
            if previous is None:
                os.environ.pop("ERAG_BLOB_DIR", None)
            else:
                os.environ["ERAG_BLOB_DIR"] = previous
            get_settings.cache_clear()
    async with get_sessionmaker()() as db:
        failed = (
            await db.execute(select(IngestionJob.error).where(IngestionJob.state != JobState.done).limit(3))
        ).all()
        if failed:
            raise EvalDatabaseError("indexing failed: " + "; ".join(str(e[0]) for e in failed))
        chunks = await db.scalar(text("SELECT count(*) FROM chunks"))
        versions = await db.scalar(select(func.count()).select_from(DocumentVersion))
    return {"documents_queued": queued, "indexed": indexed, "versions": int(versions or 0), "chunks": int(chunks or 0)}


async def prepare(mode: Mode = "strict") -> dict[str, int]:
    """Create, migrate and load the evaluation database. Call `switch_database()` first."""
    url = get_settings().database_url
    await ensure_database(url)
    embedder = build_embedder(mode)
    try:
        stats = await load_corpus(embedder)
    finally:
        await dispose_engine()
    if mode == "record" and embedder.recorded:
        embedder.save()
    stats["embeddings_recorded"] = embedder.recorded
    return stats

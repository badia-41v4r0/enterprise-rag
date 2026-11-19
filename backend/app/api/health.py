"""Health (R19, PLAN-v0.5 5.5).

- `/health/live`: the process answers. A failure means restart it.
- `/health/ready`: whether this replica should receive traffic. 503 when a required dependency is down (the
  database, the query embedding endpoint). Other checks are reported as warnings without taking the replica
  out of rotation, because restarting it would not fix them: the reranker (search falls back to the fused
  order), the workers (judged by the age of the oldest job ready to run) and the generation queue.
- `/health`: the historical endpoint, kept for existing probes.

No check exposes a question, a document or a credential; only states and numbers.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.models import GenerationWaiter

router = APIRouter(tags=["health"])

QUEUE_AGE_WARN_SECONDS = 15 * 60
_EMBED_CACHE: dict[str, tuple[float, bool, str]] = {}
_EMBED_TTL = 15.0


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health must report, not raise
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _embedding_endpoint() -> tuple[bool, str]:
    """Reachability of the query embedding endpoint, cached for a few seconds (probes are frequent)."""
    s = get_settings()
    url = (s.query_embed_url or s.ollama_base_url).rstrip("/")
    cached = _EMBED_CACHE.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < _EMBED_TTL:
        return cached[1], cached[2]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{url}/api/version")
            ok, detail = r.status_code < 500, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        ok, detail = False, type(exc).__name__
    _EMBED_CACHE[url] = (now, ok, detail)
    return ok, detail


@router.get("/health/ready", response_model=None)
async def ready(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    checks: dict[str, dict[str, Any]] = {}
    required_ok = True

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": type(exc).__name__}
        required_ok = False

    ok, detail = await _embedding_endpoint()
    checks["embedding_endpoint"] = {"ok": ok, "detail": detail}
    required_ok = required_ok and ok

    if checks["database"]["ok"]:
        from app.rag import reranker
        from app.rag.settings import load as load_retrieval_settings

        cfg = await load_retrieval_settings(db)
        if cfg.reranker:
            st = reranker.status(cfg.reranker)
            checks["reranker"] = {
                "ok": bool(st["installed"]) and not st["error"],
                "warning": True,
                "model": cfg.reranker,
                "installed": st["installed"],
                "loaded": st["loaded"],
            }
        oldest = await db.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM ingestion_jobs "
                "WHERE state = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now())"
            )
        )
        age = float(oldest or 0.0)
        checks["ingestion_queue"] = {"ok": age < QUEUE_AGE_WARN_SECONDS, "warning": True, "oldest_ready_seconds": age}
        waiting = await db.scalar(select(func.count()).select_from(GenerationWaiter))
        checks["generation_queue"] = {"ok": True, "warning": True, "waiting": int(waiting or 0)}

    status = "ok" if required_ok and all(c["ok"] for c in checks.values()) else "degraded"
    if not required_ok:
        status = "unavailable"
    return JSONResponse({"status": status, "checks": checks}, status_code=200 if required_ok else 503)

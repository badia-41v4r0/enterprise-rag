"""Explicit Ollama model downloads from the app (PLAN-v0.4 decision 3, ADR 0039).

Nothing is ever downloaded automatically. A download happens only when an administrator presses Download on one
specific model, after a confirmation that shows the model name, its size (read from the model's manifest in the
Ollama registry) and that it comes from the Ollama registry. The pull itself is done by the local Ollama
(`POST /api/pull`); this module starts it, reports progress and can cancel it (closing the stream stops the pull).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.netguard import BlockedDestination, PinnedTransport
from app.services.envcheck import ollama_client

log = logging.getLogger(__name__)

# Ollama library names: `name`, `name:tag`, `namespace/name[:tag]`. Other registries (hf.co/...) are not offered.
MODEL_NAME = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,63}(?::[A-Za-z0-9._-]{1,64})?$")
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
MAX_MANIFEST_BYTES = 200_000

# Suggestions shown in the models step. Every one is under a permissive license (checked before listing them):
# bge-m3 (MIT), Qwen3 (Apache-2.0), all-minilm / all-MiniLM-L6-v2 (Apache-2.0). Sizes are fetched at download time.
SUGGESTED = [
    {"name": "bge-m3", "purpose": "embedding", "license": "MIT", "note": "The embedding model EnterpriseRAG expects."},
    {
        "name": "qwen3:4b-instruct-2507-q4_K_M",
        "purpose": "chat",
        "license": "Apache-2.0",
        "note": "Small local chat model (about 2.5 GB) that runs on a 6 GB GPU or a CPU.",
    },
]


class ModelDownloadError(Exception):
    pass


def validate_name(name: str) -> str:
    name = name.strip()
    if not MODEL_NAME.match(name):
        raise ModelDownloadError("not an Ollama library model name (for example `bge-m3` or `qwen3:4b`)")
    return name


def _split(name: str) -> tuple[str, str]:
    repo, _, tag = name.partition(":")
    if "/" not in repo:
        repo = f"library/{repo}"
    return repo, tag or "latest"


async def registry_size(name: str, settings: Settings | None = None) -> int:
    """Total size in bytes of a model, from its manifest in the Ollama registry. Public HTTPS only, through the
    destination guard (no private addresses, no redirects followed)."""
    s = settings or get_settings()
    repo, tag = _split(validate_name(name))
    base = s.ollama_registry_url.rstrip("/")
    if not base.startswith("https://"):
        raise ModelDownloadError("the Ollama registry must be reached over https")
    url = f"{base}/v2/{repo}/manifests/{tag}"
    transport = PinnedTransport([])  # public addresses only
    try:
        async with (
            httpx.AsyncClient(transport=transport, timeout=15.0, follow_redirects=False) as client,
            client.stream("GET", url, headers={"Accept": MANIFEST_ACCEPT}) as resp,
        ):
            if resp.status_code == 404:
                raise ModelDownloadError(f"{name} was not found in the Ollama registry")
            if resp.status_code >= 300:
                raise ModelDownloadError(f"the Ollama registry answered HTTP {resp.status_code}")
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_MANIFEST_BYTES:
                    raise ModelDownloadError("manifest too large")
    except (httpx.HTTPError, BlockedDestination) as exc:
        raise ModelDownloadError(f"could not reach the Ollama registry ({type(exc).__name__})") from exc
    try:
        manifest = json.loads(body)
        layers = manifest.get("layers", [])
        total = sum(int(layer.get("size", 0)) for layer in layers) + int(manifest.get("config", {}).get("size", 0))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ModelDownloadError("the Ollama registry returned an unexpected manifest") from exc
    if total <= 0:
        raise ModelDownloadError("the model size could not be determined")
    return total


@dataclass
class Pull:
    id: str
    model: str
    size_bytes: int
    user_id: uuid.UUID | None
    status: str = "running"  # running | done | failed | cancelled
    message: str = "starting"
    completed: int = 0
    total: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    task: asyncio.Task[None] | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "message": self.message,
            "completed": self.completed,
            "total": self.total or self.size_bytes,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_PULLS: dict[str, Pull] = {}


def pulls() -> list[dict[str, Any]]:
    return [p.view() for p in sorted(_PULLS.values(), key=lambda p: p.started_at, reverse=True)][:10]


def get_pull(pull_id: str) -> Pull | None:
    return _PULLS.get(pull_id)


async def _audit(action: str, pull: Pull, **details: Any) -> None:
    from app.core.db import get_sessionmaker
    from app.services.audit import audit

    try:
        async with get_sessionmaker()() as db:
            audit(db, action, user_id=pull.user_id, resource=f"ollama-model:{pull.model}", details=details)
            await db.commit()
    except Exception:
        log.exception("could not audit %s", action)


async def _run(pull: Pull, settings: Settings) -> None:
    digests: dict[str, tuple[int, int]] = {}
    try:
        async with ollama_client(timeout=None) as client:
            url = f"{settings.ollama_base_url.rstrip('/')}/api/pull"
            async with client.stream("POST", url, json={"model": pull.model, "stream": True}) as resp:
                if resp.status_code >= 300:
                    raise ModelDownloadError(f"Ollama answered HTTP {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("error"):
                        raise ModelDownloadError(str(ev["error"])[:300])
                    pull.message = str(ev.get("status", ""))[:200]
                    if ev.get("digest") and ev.get("total"):
                        digests[str(ev["digest"])] = (int(ev.get("completed") or 0), int(ev["total"]))
                        pull.completed = sum(c for c, _ in digests.values())
                        pull.total = sum(t for _, t in digests.values())
                    if ev.get("status") == "success":
                        pull.status = "done"
        if pull.status != "done":
            raise ModelDownloadError("Ollama ended the download without success")
        pull.completed = pull.total = max(pull.total, pull.completed)
        await _audit("setup.model.download.done", pull, size_bytes=pull.size_bytes)
    except asyncio.CancelledError:
        pull.status = "cancelled"
        pull.message = "cancelled by the administrator"
        await _audit("setup.model.download.cancel", pull)
        raise
    except (ModelDownloadError, httpx.HTTPError, BlockedDestination) as exc:
        pull.status = "failed"
        pull.message = str(exc)[:300] or type(exc).__name__
        await _audit("setup.model.download.failed", pull, error=pull.message)
    finally:
        pull.finished_at = datetime.now(UTC).isoformat()


def start_pull(model: str, size_bytes: int, user_id: uuid.UUID | None, settings: Settings | None = None) -> Pull:
    """Start a download the administrator confirmed. One running download per model."""
    s = settings or get_settings()
    model = validate_name(model)
    for p in _PULLS.values():
        if p.model == model and p.status == "running":
            return p
    pull = Pull(id=uuid.uuid4().hex, model=model, size_bytes=size_bytes, user_id=user_id)
    _PULLS[pull.id] = pull
    pull.task = asyncio.create_task(_run(pull, s), name=f"ollama-pull-{model}")
    return pull


async def cancel_pull(pull_id: str) -> Pull | None:
    pull = _PULLS.get(pull_id)
    if pull is None or pull.task is None or pull.status != "running":
        return pull
    pull.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pull.task
    return pull

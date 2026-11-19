"""Environment checks for setup step 1 and the setup assistant (ADR 0038).

Read-only. Every check reports a status (ok, warn, fail, info), a short detail and the exact fix; never a value
(no secret, key, token or password is ever returned). The wizard verifies environment-level settings but never
edits them.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import CryptoError, load_master_key
from app.core.netguard import BlockedDestination, PinnedTransport, provider_checker

Check = dict[str, Any]


def _check(cid: str, label: str, status: str, detail: str = "", fix: str = "") -> Check:
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix if status != "ok" else ""}


def alembic_head() -> str | None:
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.core.config import REPO_ROOT

    ini = REPO_ROOT / "alembic.ini"
    if not ini.exists():  # installed elsewhere: look for the migrations next to the package
        ini = Path.cwd() / "alembic.ini"
    if not ini.exists():
        return None
    return ScriptDirectory.from_config(Config(str(ini))).get_current_head()


async def check_database(db: AsyncSession) -> Check:
    label = "Database reachable and migrations current"
    try:
        current = await db.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:  # noqa: BLE001 - report, never raise
        await db.rollback()
        return _check(
            "database",
            label,
            "fail",
            "The database cannot be queried.",
            "Check that PostgreSQL is running and ERAG_DATABASE_URL is right, then restart the app.",
        )
    head = alembic_head()
    if head is not None and current != head:
        return _check(
            "database",
            label,
            "fail",
            f"Database at migration {current}, code expects {head}.",
            "Run `alembic upgrade head` (the app container does it at start: restart it).",
        )
    return _check("database", label, "ok", f"Migration {current}.")


def check_master_key(s: Settings) -> Check:
    label = "Credential store master key present"
    try:
        load_master_key(s)
    except CryptoError as exc:
        return _check(
            "master_key",
            label,
            "fail",
            f"The master key cannot be loaded ({exc}).",
            "Run `erag init` (or `erag secrets init --file <path>`) and set ERAG_SECRETS_KEY or "
            "ERAG_SECRETS_KEY_FILE, then restart.",
        )
    configured = (
        s.secrets_key is not None and bool(s.secrets_key.get_secret_value())
    ) or s.secrets_key_file is not None
    if not configured:
        return _check(
            "master_key",
            label,
            "warn",
            "No master key configured: a development key derived from the session secret is in use.",
            "Run `erag init` for a real installation (production refuses to start without a master key).",
        )
    if s.secrets_key_file is not None and (s.secrets_key is None or not s.secrets_key.get_secret_value()):
        mode = s.secrets_key_file.stat().st_mode & 0o777
        if mode & 0o077:
            return _check(
                "master_key",
                label,
                "warn",
                f"The key file is readable by other users (mode {mode:o}).",
                f"chmod 600 {s.secrets_key_file}",
            )
    return _check(
        "master_key",
        label,
        "ok",
        "Loaded. Back it up outside this machine: losing it means re-entering "
        "every stored secret and re-enrolling MFA.",
    )


def check_production(s: Settings) -> Check:
    if s.is_prod:
        return _check("production", "Production mode", "ok", "ERAG_ENV=prod.")
    return _check(
        "production",
        "Production mode",
        "warn",
        f"ERAG_ENV={s.env}: development defaults are accepted.",
        "Set ERAG_ENV=prod in the environment file (erag init does) and restart.",
    )


def check_secrets(s: Settings) -> Check:
    label = "Session, MCP context and runner secrets are not the development defaults"
    weak = s.weak_secrets()
    if weak:
        return _check(
            "secrets",
            label,
            "fail",
            "Development default or too short: " + ", ".join(weak) + ".",
            "Generate strong values (erag init does, or `openssl rand -hex 32`) in the environment file "
            "and restart. The wizard never edits them.",
        )
    return _check("secrets", label, "ok", "All set to strong values.")


def check_https(s: Settings, scheme: str | None, client_host: str | None) -> Check:
    label = "HTTPS and secure cookies"
    local = client_host in (None, "127.0.0.1", "::1", "localhost", "testclient")
    if s.cookie_secure and scheme == "https":
        return _check("https", label, "ok", "Served over HTTPS with secure cookies.")
    if s.cookie_secure:
        return _check(
            "https",
            label,
            "warn",
            "Secure cookies are on but this request did not arrive over HTTPS.",
            "Make sure the reverse proxy terminates TLS and forwards X-Forwarded-Proto.",
        )
    if not local:
        return _check(
            "https",
            label,
            "fail",
            "Reached from another machine without HTTPS: passwords and sessions travel in clear.",
            "Put the application behind HTTPS (a reverse proxy with TLS) and set "
            "ERAG_COOKIE_SECURE=true; until then keep the port on localhost or a private network.",
        )
    return _check(
        "https",
        label,
        "warn",
        "Plain HTTP on this machine (fine for setup on localhost).",
        "Before other people use it, put it behind HTTPS and set ERAG_COOKIE_SECURE=true.",
    )


def check_storage(s: Settings) -> Check:
    label = "Document storage writable"
    try:
        s.blob_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=s.blob_dir, prefix=".erag-check-", delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
    except OSError as exc:
        return _check(
            "storage",
            label,
            "fail",
            f"Cannot write to {s.blob_dir} ({exc.strerror}).",
            f"Make {s.blob_dir} writable by the application user (in Docker Compose it is the `appdata` "
            "volume), or set ERAG_BLOB_DIR.",
        )
    return _check("storage", label, "ok", f"{s.blob_dir} is writable.")


async def check_worker(db: AsyncSession, s: Settings) -> Check:
    label = "Ingestion worker running"
    raw = await db.scalar(text("SELECT value->>'at' FROM app_settings WHERE key = 'worker.heartbeat'"))
    if not raw:
        return _check(
            "worker",
            label,
            "fail",
            "The worker has never reported.",
            "Start the worker (`docker compose up -d worker`, or `erag worker`).",
        )
    age = (datetime.now(UTC) - datetime.fromisoformat(str(raw))).total_seconds()
    if age > max(120.0, 6 * s.worker_heartbeat_seconds):
        return _check(
            "worker",
            label,
            "fail",
            f"Last seen {int(age)} seconds ago.",
            "Start or restart the worker (`docker compose up -d worker`) and check its log.",
        )
    return _check("worker", label, "ok", f"Last seen {int(age)} seconds ago.")


def ollama_client(timeout: float | None = 5.0) -> httpx.AsyncClient:
    """The local Ollama, through the same destination guard as Ollama providers (private addresses allowed)."""
    transport = PinnedTransport([], checker=provider_checker(allow_private=True, require_private=False))
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)


async def ollama_models(s: Settings) -> list[dict[str, Any]]:
    """Models present in the configured Ollama (`GET /api/tags`). Raises on any failure."""
    async with ollama_client() as client:
        r = await client.get(f"{s.ollama_base_url.rstrip('/')}/api/tags")
        r.raise_for_status()
        data = r.json()
    return [
        {"name": str(m.get("name") or m.get("model")), "size": int(m.get("size") or 0)}
        for m in data.get("models", [])
        if isinstance(m, dict)
    ]


def has_model(names: set[str], model: str) -> bool:
    return model in names or f"{model}:latest" in names


async def check_ollama(s: Settings, chat_model: str | None = None) -> list[Check]:
    label = "Ollama reachable"
    try:
        models = await ollama_models(s)
    except (httpx.HTTPError, BlockedDestination, ValueError) as exc:
        return [
            _check(
                "ollama",
                label,
                "fail",
                f"Ollama at {s.ollama_base_url} did not answer ({type(exc).__name__}).",
                "Start Ollama on the host (`ollama serve`) and check ERAG_OLLAMA_BASE_URL; with Docker Compose and "
                "Ollama on 127.0.0.1 use deploy/docker-compose.hostnet.yml.",
            ),
            _check("embedding_model", "Embedding model present", "fail", "Ollama is unreachable.", "Fix Ollama first."),
            _check("chat_model", "Local chat model present", "fail", "Ollama is unreachable.", "Fix Ollama first."),
        ]
    names = {m["name"] for m in models}
    out = [_check("ollama", label, "ok", f"{len(models)} model(s) at {s.ollama_base_url}.")]
    if has_model(names, s.embed_model):
        out.append(_check("embedding_model", "Embedding model present", "ok", f"{s.embed_model} is installed."))
    else:
        out.append(
            _check(
                "embedding_model",
                "Embedding model present",
                "fail",
                f"{s.embed_model} is not installed.",
                f"Download it in step 5 (Models) or run `ollama pull {s.embed_model}` on the host.",
            )
        )
    wanted = chat_model or s.local_chat_model
    if has_model(names, wanted):
        out.append(_check("chat_model", "Local chat model present", "ok", f"{wanted} is installed."))
    else:
        out.append(
            _check(
                "chat_model",
                "Local chat model present",
                "fail",
                f"{wanted} is not installed.",
                f"Choose or download a chat model in step 5 (Models), or run `ollama pull {wanted}`.",
            )
        )
    return out


async def check_runner(s: Settings) -> Check:
    label = "Isolated MCP runner (optional)"
    if not s.runner_url:
        return _check("runner", label, "info", "Not configured. Only needed to run MCP servers in containers.")
    from app.runner.client import RunnerClient, RunnerError

    try:
        images = await RunnerClient(s).images()
    except (RunnerError, httpx.HTTPError) as exc:
        return _check(
            "runner",
            label,
            "warn",
            f"Configured but not reachable ({type(exc).__name__}).",
            "Start the `runner` service, or unset ERAG_RUNNER_URL if you do not use it.",
        )
    return _check("runner", label, "ok", f"Reachable; {len(images)} approved image(s).")


async def run_checks(
    db: AsyncSession,
    settings: Settings | None = None,
    *,
    scheme: str | None = None,
    client_host: str | None = None,
) -> list[Check]:
    s = settings or get_settings()
    chat_model = await default_ollama_model(db)
    checks = [
        await check_database(db),
        check_master_key(s),
        check_production(s),
        check_secrets(s),
        check_https(s, scheme, client_host),
        check_storage(s),
        await check_worker(db, s),
        *await check_ollama(s, chat_model),
        await check_runner(s),
    ]
    return checks


async def default_ollama_model(db: AsyncSession) -> str | None:
    """The default chat model when it is a model of the built-in Ollama provider (read without building the
    router, so no stored key is decrypted)."""
    try:
        row = await db.scalar(
            text(
                "SELECT m.model FROM llm_models m JOIN llm_providers p ON p.id = m.provider_id "
                "WHERE m.is_default AND p.builtin AND p.slug = 'ollama'"
            )
        )
    except Exception:  # noqa: BLE001 - the check reports, it never raises
        await db.rollback()
        return None
    return str(row) if row else None


async def default_ollama_model_or_env(s: Settings) -> str:
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        return await default_ollama_model(db) or s.local_chat_model


def summary(checks: list[Check]) -> dict[str, int]:
    out = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for c in checks:
        out[c["status"]] = out.get(c["status"], 0) + 1
    return out

"""Shared fixtures.

Tests run against a real PostgreSQL + pgvector database (`erag_test`), created on demand.
Tests that need Postgres are skipped when it is unreachable. Nothing here mocks an LLM.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get("ERAG_TEST_DATABASE_URL", "postgresql+asyncpg://erag:erag@localhost:55432/erag_test")
os.environ["ERAG_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("ERAG_BLOB_DIR", tempfile.mkdtemp(prefix="erag-blobs-"))
os.environ.setdefault("ERAG_MCP_TICKETS_DB", str(Path(tempfile.mkdtemp(prefix="erag-tickets-")) / "tickets.sqlite3"))
os.environ.setdefault("ERAG_LOG_JSON", "false")
os.environ.setdefault("ERAG_CONNECTOR_ROOTS", tempfile.mkdtemp(prefix="erag-connector-root-"))
os.environ.setdefault("ERAG_FRONTEND_DIST", str(ROOT / "nonexistent-dist"))
os.environ.setdefault("ERAG_SETUP_TOKEN_FILE", str(Path(tempfile.mkdtemp(prefix="erag-setup-")) / "setup-token"))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402


def _admin_url(url: str) -> str:
    base, _, _ = url.rpartition("/")
    return base + "/postgres"


async def _ensure_database() -> bool:
    dbname = TEST_DB_URL.rsplit("/", 1)[1]
    engine = create_async_engine(_admin_url(TEST_DB_URL), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        return True
    except (OSError, Exception):  # noqa: BLE001
        return False
    finally:
        await engine.dispose()


_DB_STATE: dict[str, bool] = {}


def db_available() -> bool:
    if "ok" not in _DB_STATE:
        ok = asyncio.run(_ensure_database())
        if ok:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=ROOT,
                check=True,
                env={**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL},
                capture_output=True,
            )
        _DB_STATE["ok"] = ok
    return _DB_STATE["ok"]


def ollama_has(model: str) -> bool:
    key = f"ollama:{model}"
    if key not in _DB_STATE:
        try:
            r = httpx.get(f"{get_settings().ollama_base_url}/api/tags", timeout=2)
            names = {m["name"] for m in r.json().get("models", [])}
            _DB_STATE[key] = model in names or f"{model}:latest" in names
        except (httpx.HTTPError, ValueError):
            _DB_STATE[key] = False
    return _DB_STATE[key]


def docker_available() -> bool:
    if "docker" not in _DB_STATE:
        sock = Path(os.environ.get("ERAG_RUNNER_DOCKER_SOCKET", "/var/run/docker.sock"))
        try:
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=2) as c:
                _DB_STATE["docker"] = c.get("http://docker/_ping").status_code == 200
        except (httpx.HTTPError, OSError):
            _DB_STATE["docker"] = False
    return _DB_STATE["docker"]


def open_jev_available() -> bool:
    if "open_jev" not in _DB_STATE:
        url = os.environ.get("ERAG_TEST_OPEN_JEV_URL", "")
        try:
            _DB_STATE["open_jev"] = bool(url) and httpx.get(f"{url.rstrip('/')}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            _DB_STATE["open_jev"] = False
    return _DB_STATE["open_jev"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    settings = get_settings()
    for item in items:
        if "db" in item.keywords and not db_available():
            item.add_marker(pytest.mark.skip(reason="PostgreSQL test database unreachable"))
        if "embeddings" in item.keywords and not ollama_has(settings.embed_model):
            item.add_marker(pytest.mark.skip(reason=f"Ollama with {settings.embed_model} unreachable"))
        if "provider_ollama" in item.keywords and not ollama_has(settings.local_chat_model):
            item.add_marker(pytest.mark.skip(reason=f"Ollama with {settings.local_chat_model} unreachable"))
        if "provider_openrouter" in item.keywords and settings.openrouter_api_key is None:
            item.add_marker(pytest.mark.skip(reason="ERAG_OPENROUTER_API_KEY not set"))
        if "docker" in item.keywords and not docker_available():
            item.add_marker(pytest.mark.skip(reason="Docker daemon unreachable"))
        if "decider_open" in item.keywords and not open_jev_available():
            item.add_marker(pytest.mark.skip(reason="set ERAG_TEST_OPEN_JEV_URL to a running open Jev-style server"))
        if "decider_official" in item.keywords and not os.environ.get("ERAG_TEST_JEV_API_KEY"):
            item.add_marker(pytest.mark.skip(reason="set ERAG_TEST_JEV_API_KEY to run (real, paid calls)"))
        if "ollama_download" in item.keywords and os.environ.get("ERAG_TEST_OLLAMA_DOWNLOAD") != "1":
            item.add_marker(pytest.mark.skip(reason="set ERAG_TEST_OLLAMA_DOWNLOAD=1 to download all-minilm for real"))
        if "provider_claude_code" in item.keywords and os.environ.get("ERAG_RUN_CLAUDE_CODE_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="set ERAG_RUN_CLAUDE_CODE_TESTS=1 to run"))


TABLES = [
    "setup_state",
    "audit_log",
    "sync_runs",
    "connector_items",
    "connectors",
    "secrets",
    "app_settings",
    "mfa_recovery_codes",
    "provider_spend",
    "provider_daily_usage",
    "llm_usage",
    "quota_pool_usage",
    "quotas",
    "router_decisions",
    "router_lanes",
    "router_deciders",
    "llm_models",
    "llm_providers",
    "messages",
    "conversations",
    "ingestion_jobs",
    "chunks",
    "grants",
    "tools",
    "mcp_servers",
    "user_roles",
    "sessions",
    "roles",
    "users",
    "document_versions",
    "documents",
    "containers",
]


async def truncate_all() -> None:
    from app.llm import registry
    from app.llm.router import invalidate_router_cache
    from app.services.setup import invalidate_cache

    invalidate_router_cache()
    invalidate_cache()
    registry._cloud_override.clear()
    async with get_sessionmaker()() as s:
        await s.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
        await s.commit()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A clean database for each test."""
    await truncate_all()
    async with get_sessionmaker()() as session:
        yield session
    await truncate_all()


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engine() -> AsyncIterator[None]:
    yield
    await dispose_engine()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

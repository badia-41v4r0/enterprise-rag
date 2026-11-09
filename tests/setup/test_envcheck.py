"""Environment checks against real failures (PLAN-v0.4 section 9, ADR 0038): an unreachable Ollama URL, a missing
model, a default secret, a missing master key, a non-writable storage path, a silent worker, a runner that does not
answer. Real sockets, real Ollama, real Postgres and a real filesystem; nothing is mocked."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import envcheck
from app.services.setup import worker_heartbeat
from tests.setup.helpers import reset_limiters, through_admin

pytestmark = pytest.mark.db


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _by_id(checks: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(c["id"]): c for c in checks}


async def test_unreachable_ollama_is_a_failure_with_the_fix() -> None:
    s = get_settings().model_copy(update={"ollama_base_url": f"http://127.0.0.1:{_closed_port()}"})
    checks = _by_id(await envcheck.check_ollama(s))
    assert checks["ollama"]["status"] == "fail" and "ollama serve" in str(checks["ollama"]["fix"])
    assert checks["embedding_model"]["status"] == "fail" and checks["chat_model"]["status"] == "fail"


@pytest.mark.embeddings
async def test_missing_models_are_reported_against_the_real_ollama() -> None:
    s = get_settings().model_copy(update={"embed_model": "erag-no-such-embedder"})
    checks = _by_id(await envcheck.check_ollama(s, chat_model="erag-no-such-chat:1b"))
    assert checks["ollama"]["status"] == "ok"
    assert checks["embedding_model"]["status"] == "fail"
    assert "ollama pull erag-no-such-embedder" in str(checks["embedding_model"]["fix"])
    assert checks["chat_model"]["status"] == "fail"
    ok = _by_id(await envcheck.check_ollama(get_settings()))
    assert ok["embedding_model"]["status"] == "ok"


def test_default_and_weak_secrets_fail_and_values_are_never_shown() -> None:
    s = get_settings().model_copy(update={"secret_key": get_settings().secret_key})
    c = envcheck.check_secrets(s)
    assert c["status"] == "fail" and "ERAG_SECRET_KEY" in str(c["detail"])
    assert "dev-insecure" not in json.dumps(c)
    strong = get_settings().model_copy(
        update={
            "secret_key": type(s.secret_key)("s" * 40),
            "mcp_context_secret": type(s.secret_key)("m" * 40),
            "runner_url": "http://127.0.0.1:1",
            "runner_token": type(s.secret_key)("short"),
        }
    )
    c = envcheck.check_secrets(strong)
    assert c["status"] == "fail" and c["detail"] == "Development default or too short: ERAG_RUNNER_TOKEN."
    assert "short" not in str(c["fix"])


def test_missing_master_key_fails_and_open_permissions_warn(tmp_path: Path) -> None:
    s = get_settings().model_copy(update={"secrets_key": None, "secrets_key_file": tmp_path / "absent.key"})
    c = envcheck.check_master_key(s)
    assert c["status"] == "fail" and "erag init" in str(c["fix"])
    from app.core.crypto import generate_key, write_key_file

    key = tmp_path / "master.key"
    write_key_file(key, generate_key())
    s = s.model_copy(update={"secrets_key_file": key})
    assert envcheck.check_master_key(s)["status"] == "ok"
    os.chmod(key, 0o644)
    c = envcheck.check_master_key(s)
    assert c["status"] == "warn" and "chmod 600" in str(c["fix"])
    assert key.read_text().strip() not in json.dumps(c)
    dev = get_settings().model_copy(update={"secrets_key": None, "secrets_key_file": None})
    assert envcheck.check_master_key(dev)["status"] == "warn"  # dev-derived key


def test_storage_that_cannot_be_written_fails(tmp_path: Path) -> None:
    ro = tmp_path / "readonly"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        c = envcheck.check_storage(get_settings().model_copy(update={"blob_dir": ro / "blobs"}))
        assert c["status"] == "fail" and "writable" in str(c["fix"])
        c = envcheck.check_storage(get_settings().model_copy(update={"blob_dir": ro}))
        assert c["status"] == "fail"
    finally:
        os.chmod(ro, 0o700)
    assert envcheck.check_storage(get_settings().model_copy(update={"blob_dir": tmp_path / "ok"}))["status"] == "ok"


def test_production_and_https_checks() -> None:
    s = get_settings()
    assert envcheck.check_production(s)["status"] == "warn"
    assert envcheck.check_production(s.model_copy(update={"env": "prod"}))["status"] == "ok"
    assert envcheck.check_https(s, "http", "127.0.0.1")["status"] == "warn"
    assert envcheck.check_https(s, "http", "203.0.113.9")["status"] == "fail"  # exposed without HTTPS
    secure = s.model_copy(update={"cookie_secure": True})
    assert envcheck.check_https(secure, "https", "203.0.113.9")["status"] == "ok"
    assert envcheck.check_https(secure, "http", "203.0.113.9")["status"] == "warn"


async def test_runner_optional_and_unreachable() -> None:
    s = get_settings().model_copy(update={"runner_url": None})
    assert (await envcheck.check_runner(s))["status"] == "info"
    s = get_settings().model_copy(
        update={"runner_url": f"http://127.0.0.1:{_closed_port()}", "runner_token": get_settings().secret_key}
    )
    assert (await envcheck.check_runner(s))["status"] == "warn"


async def test_worker_heartbeat_and_database_check(db: AsyncSession) -> None:
    s = get_settings()
    assert (await envcheck.check_worker(db, s))["status"] == "fail"
    await worker_heartbeat(db)
    assert (await envcheck.check_worker(db, s))["status"] == "ok"
    c = await envcheck.check_database(db)
    assert c["status"] == "ok" and envcheck.alembic_head() in str(c["detail"])


async def test_environment_endpoint_reports_booleans_and_fixes_only(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    reset_limiters()
    token = await through_admin(db, client)
    r = await client.get("/api/setup/environment")
    assert r.status_code == 200
    body = r.json()
    ids = [c["id"] for c in body["checks"]]
    assert ids == [
        "database",
        "master_key",
        "production",
        "secrets",
        "https",
        "storage",
        "worker",
        "ollama",
        "embedding_model",
        "chat_model",
        "runner",
    ]
    assert all(c["status"] in ("ok", "warn", "fail", "info") for c in body["checks"])
    text = r.text
    s = get_settings()
    for secret in (s.secret_key.get_secret_value(), s.mcp_context_secret.get_secret_value(), token):
        assert secret not in text
    assert all((c["fix"] == "") == (c["status"] in ("ok", "info")) for c in body["checks"])
    # Without the setup session the check is not available.
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    assert (await anon.get("/api/setup/environment")).status_code == 401
    await anon.aclose()

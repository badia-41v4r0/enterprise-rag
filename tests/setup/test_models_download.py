"""Models step against the real local Ollama and the real Ollama registry (ADR 0039).

Nothing is downloaded unless the Download action is called: every other models endpoint, the environment check and
the Test buttons leave the model list unchanged. The real download is opt-in (`ERAG_TEST_OLLAMA_DOWNLOAD=1`): it
downloads the small `all-minilm` embedding model (about 46 MB, Apache-2.0) through the endpoint, cancels a second
download, and removes the model with `ollama rm` so the machine's model list is unchanged afterwards."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services import ollama_models
from tests.setup.helpers import reset_limiters, through_admin

pytestmark = [pytest.mark.db, pytest.mark.embeddings]

SMALL = "all-minilm"


def _names() -> set[str]:
    r = httpx.get(f"{get_settings().ollama_base_url}/api/tags", timeout=5)
    return {m["name"] for m in r.json()["models"]}


async def _registry_reachable() -> bool:
    try:
        return await ollama_models.registry_size(SMALL) > 0
    except Exception:  # noqa: BLE001
        return False


def _rm(name: str) -> None:
    if shutil.which("ollama"):
        subprocess.run(["ollama", "rm", name], capture_output=True, check=False)
    else:
        httpx.request("DELETE", f"{get_settings().ollama_base_url}/api/delete", json={"model": name}, timeout=30)


@pytest.fixture(autouse=True)
def _limits() -> None:
    reset_limiters()


async def test_nothing_is_downloaded_without_the_download_action(db: AsyncSession, client: httpx.AsyncClient) -> None:
    before = _names()
    assert not any(n.startswith(SMALL) for n in before), "all-minilm must not be installed before this test"
    await through_admin(db, client)
    st = (await client.get("/api/setup/models")).json()
    assert st["reachable"] and st["embedding"]["installed"]
    assert {s["name"] for s in st["suggested"]} == {"bge-m3", "qwen3:4b-instruct-2507-q4_K_M"}
    assert (await client.get("/api/setup/environment")).status_code == 200
    r = await client.post("/api/setup/models/test", json={"kind": "embedding"})
    assert r.json()["ok"] and r.json()["dimensions"] == 1024
    # A chat Test with a model that is not installed reports it; Ollama does not pull it on this request.
    r = await client.post("/api/setup/models/test", json={"kind": "chat", "model": SMALL})
    assert r.json()["ok"] is False
    # Invalid names never reach the registry.
    for bad in ("hf.co/x/y:q4", "../etc", "UPPER", "a b"):
        assert (await client.post("/api/setup/models/download/preview", json={"model": bad})).status_code == 409
    if await _registry_reachable():
        r = await client.post("/api/setup/models/download/preview", json={"model": SMALL})
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["model"] == SMALL and 30_000_000 < p["size_bytes"] < 100_000_000 and "Ollama registry" in p["source"]
        assert p["already_installed"] is False
        # The download itself needs explicit confirmation of that exact size.
        body = {"model": SMALL, "size_bytes": p["size_bytes"], "confirm": False}
        assert (await client.post("/api/setup/models/download", json=body)).status_code == 422
        body = {"model": SMALL, "size_bytes": p["size_bytes"] + 1, "confirm": True}
        assert (await client.post("/api/setup/models/download", json=body)).status_code == 409
    assert _names() == before


@pytest.mark.provider_ollama
async def test_choose_the_chat_model(db: AsyncSession, client: httpx.AsyncClient) -> None:
    s = get_settings()
    await through_admin(db, client)
    r = await client.post("/api/setup/models/chat", json={"model": s.local_chat_model})
    assert r.status_code == 200 and r.json()["default_model_id"] == f"ollama:{s.local_chat_model}"
    st = (await client.get("/api/setup/models")).json()
    assert st["chat"]["default_model"] == s.local_chat_model and st["chat"]["installed"]
    assert (await client.post("/api/setup/models/chat", json={"model": "erag-not-installed:1b"})).status_code == 409
    assert (await client.post("/api/setup/models/chat", json={"model": s.embed_model})).status_code == 422
    r = await client.post("/api/setup/models/test", json={"kind": "chat"})
    assert r.json()["ok"] is True and r.json()["model"] == s.local_chat_model


@pytest.mark.ollama_download
async def test_explicit_download_with_progress_cancel_and_cleanup(db: AsyncSession, client: httpx.AsyncClient) -> None:
    before = _names()
    assert not any(n.startswith(SMALL) for n in before)
    await through_admin(db, client)
    p = (await client.post("/api/setup/models/download/preview", json={"model": SMALL})).json()
    body = {"model": SMALL, "size_bytes": p["size_bytes"], "confirm": True}
    try:
        # A download that is cancelled: the model does not appear.
        r = await client.post("/api/setup/models/download", json=body)
        assert r.status_code == 202
        pull_id = r.json()["id"]
        r = await client.delete(f"/api/setup/models/download/{pull_id}")
        assert r.json()["status"] in ("cancelled", "done")
        cancelled = r.json()["status"] == "cancelled"
        if cancelled:
            assert not any(n.startswith(SMALL) for n in _names())
        # A complete download, with progress.
        r = await client.post("/api/setup/models/download", json=body)
        pull_id = r.json()["id"]
        seen_progress = False
        for _ in range(600):
            v = (await client.get(f"/api/setup/models/download/{pull_id}")).json()
            seen_progress = seen_progress or v["completed"] > 0
            if v["status"] != "running":
                break
            await asyncio.sleep(0.5)
        assert v["status"] == "done", v
        assert v["completed"] >= p["size_bytes"] * 0.9
        assert any(n.startswith(SMALL) for n in _names())
        st = (await client.get("/api/setup/models")).json()
        assert any(m["name"].startswith(SMALL) for m in st["installed"])
        print(f"cancelled first: {cancelled}; progress seen: {seen_progress}; size {p['size_bytes']}")
    finally:
        _rm(SMALL)
        _rm(f"{SMALL}:latest")
    assert _names() == before

"""Isolated runner (R5) against a REAL Docker daemon, with the real office MCP image.

The runner app runs in-process (as the separate service would) and drives Docker over its socket.
Confinement is probed from INSIDE the containers: no network, read-only root, capabilities dropped,
no-new-privileges, seccomp on, limits applied, only its own socket visible, secrets only where assigned.
Containers are named erag-mcp-erag-test-* and removed afterwards.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.mcp.gateway as gateway_module
import app.runner.client as runner_client
from app.authz.policy import effective_permissions
from app.core.config import get_settings
from app.llm.providers.base import ToolCall
from app.mcp.tool_loop import McpToolRuntime
from app.models import Action, AuditLog, Classification, Grant, McpServer, ResourceType, ServerStatus, Tool, User
from app.runner.engine import DockerEngine
from app.runner.service import Runner, create_runner_app, redact
from app.services import vault
from tests.conftest import ROOT
from tests.factories import make_role, make_user

pytestmark = [pytest.mark.db, pytest.mark.docker]
IMAGE = "erag-mcp-office:0.2"


def _image_id() -> str:
    out = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], capture_output=True, text=True)
    if out.returncode != 0:  # build the approved image once (real build, cached afterwards)
        subprocess.run(
            ["docker", "build", "-q", "-f", "deploy/mcp-office.Dockerfile", "-t", IMAGE, "."], cwd=ROOT, check=True
        )
        out = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], capture_output=True, text=True
        )
    return out.stdout.strip()


@pytest.fixture
async def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Runner, DockerEngine]]:
    s = get_settings()
    (tmp_path / "mounts" / "kb").mkdir(parents=True)
    (tmp_path / "mounts" / "kb" / "note.txt").write_text("read-only knowledge")
    sockets = Path("/tmp") / f"erag-test-sockets-{uuid.uuid4().hex[:8]}"
    for k, v in {
        "runner_url": "http://runner",
        "runner_token": SecretStr("runner-test-token"),
        "runner_images": [f"office={await asyncio.to_thread(_image_id)}", "tampered=sha256:" + "0" * 64],
        "runner_socket_dir": sockets,
        "runner_mount_roots": [tmp_path / "mounts"],
    }.items():
        monkeypatch.setattr(s, k, v)
    engine = DockerEngine(s.runner_docker_socket)
    app = create_runner_app(s, engine)
    monkeypatch.setattr(runner_client, "transport_override", httpx.ASGITransport(app=app))
    gateway_module._limiter = None
    gateway_module._tool_limiters.clear()
    yield app.state.runner, engine
    for c in await engine.list_containers("erag.runner"):
        if c["Names"][0].startswith("/erag-mcp-erag-test-"):
            await engine.remove(c["Id"])
    await engine.aclose()
    shutil.rmtree(sockets, ignore_errors=True)


async def _server(db: AsyncSession, name: str, runtime: dict[str, Any]) -> McpServer:
    s = McpServer(
        name=name,
        command="",
        builtin=False,
        transport="runner",
        status=ServerStatus.approved,
        max_classification=Classification.internal,
        result_classification=Classification.confidential,
        send_permission_context=False,
        runtime=runtime,
        limits={"timeout_seconds": 60},
    )
    db.add(s)
    await db.commit()
    return s


async def _ensure(client: httpx.AsyncClient, name: str, **spec: Any) -> httpx.Response:
    return await client.post(
        f"/v1/servers/{name}/ensure",
        json={"image": "office", **spec},
        headers={"Authorization": "Bearer runner-test-token"},
    )


async def test_runner_api_needs_its_token_and_allowlisted_pinned_images(runner: tuple[Runner, DockerEngine]) -> None:
    rn, _ = runner
    app = create_runner_app(get_settings(), rn.engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runner") as c:
        assert (await c.get("/v1/images")).status_code == 401
        assert (await c.get("/v1/images", headers={"Authorization": "Bearer wrong"})).status_code == 401
        r = await _ensure(c, "erag-test-x", image="python:3.12-slim")
        assert r.status_code == 403 and "allowlist" in r.text
        r = await _ensure(c, "erag-test-x", image="tampered")
        assert r.status_code in (403, 404)
        r = await _ensure(c, "erag-test-x", mounts=[{"source": "/etc", "target": "/data/etc"}])
        assert r.status_code == 403
        r = await _ensure(c, "erag-test-x", cpus=64)
        assert r.status_code == 422
        # There is no way to override the command of an approved image (extra fields are ignored).
        r = await _ensure(c, "erag-test-cmd", Cmd=["sh", "-c", "id"], cmd=["id"])
        assert r.status_code == 200, r.text
        info = await rn.engine.inspect("erag-mcp-erag-test-cmd")
        assert info is not None and info["Config"]["Cmd"] == [
            "python",
            "-m",
            "mcp_servers.office_server",
            "--uds",
            "/run/mcp/mcp.sock",
        ]


async def test_mcp_in_the_runner_is_confined_and_callable_through_the_gateway(
    db: AsyncSession, runner: tuple[Runner, DockerEngine], tmp_path: Path
) -> None:
    rn, engine = runner
    await vault.set_secret(db, "office_token", "S3CRET-office-0042", user_id=None)
    role = await make_role(db, "eng")
    await make_user(db, "eng@acme.test", roles=[role])
    role_id = role.id
    await db.commit()
    a = await _server(
        db,
        "erag-test-a",
        {
            "image": "office",
            "cpus": 0.5,
            "memory_mb": 128,
            "pids": 32,
            "secrets": {"ERAG_OFFICE_API_TOKEN": "office_token"},
            "mounts": [{"source": str(tmp_path / "mounts" / "kb"), "target": "/data/kb"}],
        },
    )
    b = await _server(db, "erag-test-b", {"image": "office", "memory_mb": 128})
    for srv in (a, b):
        t = Tool(server_id=srv.id, name="office_info", classification=Classification.confidential, enabled=False)
        db.add(t)
    await db.commit()
    from app.mcp.review import discover

    for srv in (a, b):
        await discover(db, srv)
        tool = await db.scalar(select(Tool).where(Tool.server_id == srv.id, Tool.name == "office_info"))
        assert tool is not None
        tool.enabled, tool.approved_hash = True, tool.content_hash
        db.add(Grant(role_id=role_id, resource_type=ResourceType.tool, resource_id=tool.id, action=Action.execute))
    await db.commit()

    # Called through the normal tool path: authz, clearance, rug-pull check, Unix socket, validator.
    u = await db.scalar(select(User).where(User.email == "eng@acme.test"))
    assert u is not None
    perms = await effective_permissions(db, u)
    rt = McpToolRuntime()
    out_a = await rt.execute(
        db, u, perms, ToolCall("1", "erag-test-a__office_info", '{"city":"madrid"}'), Classification.internal
    )
    out_b = await rt.execute(
        db, u, perms, ToolCall("2", "erag-test-b__office_info", '{"city":"berlin"}'), Classification.internal
    )
    await db.commit()
    assert out_a.ok and out_b.ok, (out_a.content, out_b.content)
    assert json.loads(out_a.content)["booking_system"] == "configured"
    assert json.loads(out_b.content)["booking_system"] == "not configured"
    assert out_a.classification == Classification.confidential
    uses = (await db.scalars(select(AuditLog).where(AuditLog.action == "secret.use"))).all()
    assert {x.details["consumer"] for x in uses} == {f"mcp:{a.id}"}

    ca = (await engine.inspect("erag-mcp-erag-test-a")) or {}
    cb = (await engine.inspect("erag-mcp-erag-test-b")) or {}
    hc = ca["HostConfig"]
    assert hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] is True and hc["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in hc["SecurityOpt"]
    assert (hc["Memory"], hc["NanoCpus"], hc["PidsLimit"]) == (128 * 1024 * 1024, 500_000_000, 32)
    assert ca["Config"]["User"] == "10001:10001"

    async def probe(cid: str, code: str) -> tuple[int, str]:
        return await engine.exec(cid, ["python", "-c", code])

    # No network: only loopback exists, and nothing outside is reachable (internet, host, Postgres).
    code, out = await probe(ca["Id"], "print(open('/proc/net/dev').read())")
    assert code == 0 and "lo:" in out and "eth0" not in out
    for host, port in (("1.1.1.1", 53), ("172.17.0.1", 55432), ("127.0.0.1", 55432), ("127.0.0.1", 8088)):
        code, out = await probe(ca["Id"], f"import socket; socket.create_connection(('{host}', {port}), timeout=2)")
        assert code != 0, (host, port, out)
    # Read-only root, writable (noexec) tmpfs, read-only admin mount.
    assert (await probe(ca["Id"], "open('/app/evil.py', 'w')"))[0] != 0
    assert (await probe(ca["Id"], "open('/tmp/scratch', 'w').write('x')"))[0] == 0
    assert (await probe(ca["Id"], "print(open('/data/kb/note.txt').read())"))[1].strip() == "read-only knowledge"
    assert (await probe(ca["Id"], "open('/data/kb/new.txt', 'w')"))[0] != 0
    # Kernel view of the process: no capabilities, no-new-privileges, seccomp filter on.
    code, out = await probe(ca["Id"], "print(open('/proc/self/status').read())")
    status_lines = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    assert status_lines["CapEff"].strip() == "0000000000000000"
    assert status_lines["NoNewPrivs"].strip() == "1" and status_lines["Seccomp"].strip() == "2"
    # Each MCP sees only its own socket directory; the secret exists only in the assigned container.
    assert (await probe(cb["Id"], "import os; print(os.listdir('/run/mcp'))"))[1].strip() == "['mcp.sock']"
    probe_env = "import os; print(os.environ.get('ERAG_OFFICE_API_TOKEN'))"
    assert (await probe(ca["Id"], probe_env))[1].strip() == "S3CRET-office-0042"
    assert (await probe(cb["Id"], probe_env))[1].strip() == "None"

    # Status and logs through the runner API; secret values are redacted.
    st = await rn.status("erag-test-a")
    assert st["network_mode"] == "none" and st["socket"] is True
    assert "S3CRET-office-0042" not in await rn.logs("erag-test-a", 100)

    # Idle shutdown removes the container; the next call starts it again transparently.
    rn.last_used["erag-test-a"] = time.monotonic() - 3600
    assert "erag-test-a" in await rn.reap_idle()
    assert await engine.inspect("erag-mcp-erag-test-a") is None
    again = await rt.execute(
        db, u, perms, ToolCall("3", "erag-test-a__office_info", '{"city":"lisbon"}'), Classification.internal
    )
    assert again.ok


def test_log_redaction() -> None:
    line = "connecting with token=abc123def and Bearer zzz999yyy; key S3CRET-office-0042 loaded"
    out = redact(line, {"S3CRET-office-0042"})
    assert "S3CRET" not in out and "abc123def" not in out and "zzz999yyy" not in out

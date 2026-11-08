"""The isolated MCP runner (ADR 0022): a separate service, the only one with the container engine socket.

    python -m app.runner.service            # listens on ERAG_RUNNER_LISTEN (default 127.0.0.1:8790)

Narrow API, authenticated with ERAG_RUNNER_TOKEN: ensure (start or reuse), stop, status, logs, images.
- Only images listed in ERAG_RUNNER_IMAGES run, pinned by image ID; no command override is accepted.
- Hardening: non-root user, read-only root filesystem, small noexec tmpfs, all capabilities dropped,
  no-new-privileges, the engine's default seccomp profile, CPU/memory/PID limits, `--init`.
- Network mode `none`: no interfaces but loopback. The gateway reaches the server over HTTP on a Unix
  socket in a per-MCP directory, the only thing mounted besides admin-approved read-only paths. So an
  MCP cannot reach Postgres, the app, the internet or another MCP (whose socket it cannot see).
- Idle containers are stopped and removed; logs are returned with secret values redacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.runner.engine import DockerEngine, Engine, EngineError

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SOCKET = "mcp.sock"
LABEL = "erag.runner"
REDACTED = "[redacted]"
_GENERIC_SECRETS = re.compile(r"(?i)(bearer\s+|token[=:]\s*|password[=:]\s*|api[_-]?key[=:]\s*)[^\s\"']+")


class Mount(BaseModel):
    source: str = Field(max_length=500)
    target: str = Field(pattern=r"^/data(/[A-Za-z0-9._-]+)+$", max_length=200)


class Spec(BaseModel):
    image: str = Field(max_length=100)
    cpus: float = Field(default=0.5, gt=0)
    memory_mb: int = Field(default=256, ge=32)
    pids: int = Field(default=64, ge=8, le=1024)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)  # values are redacted from logs
    mounts: list[Mount] = Field(default_factory=list, max_length=5)
    network: Literal["none"] = "none"  # allowlisted egress is not implemented (ADR 0022)
    idle_minutes: int | None = Field(default=None, ge=1, le=1440)


def parse_images(entries: list[str]) -> dict[str, str]:
    out = {}
    for e in entries:
        name, _, digest = e.partition("=")
        if name.strip() and digest.strip().startswith("sha256:"):
            out[name.strip()] = digest.strip()
    return out


def redact(text: str, secrets: set[str]) -> str:
    for s in sorted((x for x in secrets if len(x) >= 4), key=len, reverse=True):
        text = text.replace(s, REDACTED)
    return _GENERIC_SECRETS.sub(lambda m: m.group(1) + REDACTED, text)


class Runner:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.images = parse_images(settings.runner_images)
        self.base = settings.runner_socket_dir
        self.last_used: dict[str, float] = {}
        self.idle: dict[str, int] = {}
        self.secrets: dict[str, set[str]] = {}
        # Containers belong to the runner instance (socket directory) that created them; the reaper of
        # one runner never touches another's (e.g. a test runner next to a deployed one).
        self.instance = hashlib.sha256(str(self.base.resolve()).encode()).hexdigest()[:12]

    def container_name(self, name: str) -> str:
        return f"erag-mcp-{name}"

    def socket_path(self, name: str) -> Path:
        return self.base / name / SOCKET

    def _check(self, name: str, spec: Spec) -> str:
        if not NAME.match(name):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid server name")
        if spec.image not in self.images:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"image {spec.image!r} is not on the runner allowlist")
        if spec.cpus > self.settings.runner_max_cpus or spec.memory_mb > self.settings.runner_max_memory_mb:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "resource limits above the runner maximum")
        for k in [*spec.env, *spec.secret_env]:
            if not ENV_NAME.match(k):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid environment variable {k!r}")
        roots = [r.resolve() for r in self.settings.runner_mount_roots]
        for m in spec.mounts:
            src = Path(m.source).resolve()
            if not any(src.is_relative_to(r) for r in roots) or not src.exists():
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"mount source {m.source!r} is not in an allowed root")
        return self.images[spec.image]

    def _container_spec(self, name: str, image_id: str, spec: Spec, config_hash: str) -> dict[str, Any]:
        mounts: list[dict[str, Any]] = [
            {"Type": "bind", "Source": str(self.base / name), "Target": "/run/mcp", "ReadOnly": False}
        ]
        mounts += [
            {"Type": "bind", "Source": str(Path(m.source).resolve()), "Target": m.target, "ReadOnly": True}
            for m in spec.mounts
        ]
        env = {**spec.env, **spec.secret_env}
        return {
            "Image": image_id,
            "User": "10001:10001",
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": {LABEL: self.instance, "erag.mcp": name, "erag.config": config_hash},
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m"},  # noqa: S108 - container path
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": spec.pids,
                "Memory": spec.memory_mb * 1024 * 1024,
                "MemorySwap": spec.memory_mb * 1024 * 1024,
                "NanoCpus": int(spec.cpus * 1e9),
                "Init": True,
                "Mounts": mounts,
                "RestartPolicy": {"Name": "no"},
            },
        }

    def _prepare_dir(self, name: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base, 0o711)  # noqa: S103 - others may traverse to a known socket, not list the MCPs
        d = self.base / name
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o777)  # the container user (uid 10001) creates its socket here  # noqa: S103
        sock = d / SOCKET
        if sock.exists() or sock.is_symlink():
            sock.unlink()

    async def ensure(self, name: str, spec: Spec) -> dict[str, Any]:
        pinned = self._check(name, spec)
        # The allowlist name is only a label: the container is created from the pinned image ID, so
        # re-tagging something else with the same name changes nothing.
        image_id = await self.engine.image_id(pinned)
        if image_id is None or image_id != pinned:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"pinned image for {spec.image!r} is not present on the host"
            )
        config_hash = hashlib.sha256(json.dumps(spec.model_dump(), sort_keys=True).encode()).hexdigest()[:16]
        cname = self.container_name(name)
        current = await self.engine.inspect(cname)
        self.last_used[name] = time.monotonic()
        self.idle[name] = spec.idle_minutes or self.settings.runner_idle_minutes
        self.secrets[name] = set(spec.secret_env.values())
        if (
            current is not None
            and current["State"].get("Running")
            and current["Config"]["Labels"].get("erag.config") == config_hash
            and self.socket_path(name).exists()
        ):
            return {"container": current["Id"], "socket": str(self.socket_path(name)), "started": False}
        if current is not None:
            await self.engine.remove(current["Id"])
        self._prepare_dir(name)
        cid = await self.engine.create(cname, self._container_spec(name, pinned, spec, config_hash))
        await self.engine.start(cid)
        for _ in range(300):  # wait (up to 60 s) for the server to listen on its socket
            if self.socket_path(name).exists():
                return {"container": cid, "socket": str(self.socket_path(name)), "started": True}
            info = await self.engine.inspect(cid)
            if info is not None and not info["State"].get("Running"):
                logs = redact(await self.engine.logs(cid, 50), self.secrets[name])
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"container exited: {logs[-500:]}")
            await asyncio.sleep(0.2)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "the MCP server did not open its socket in time")

    async def stop(self, name: str) -> bool:
        info = await self.engine.inspect(self.container_name(name))
        if info is None:
            return False
        await self.engine.remove(info["Id"])
        self.last_used.pop(name, None)
        return True

    async def status(self, name: str) -> dict[str, Any]:
        info = await self.engine.inspect(self.container_name(name))
        if info is None:
            return {"state": "absent"}
        hc = info["HostConfig"]
        return {
            "state": info["State"].get("Status"),
            "container": info["Id"][:12],
            "started_at": info["State"].get("StartedAt"),
            "image": info["Config"]["Image"],
            "network_mode": hc.get("NetworkMode"),
            "read_only_rootfs": hc.get("ReadonlyRootfs"),
            "cap_drop": hc.get("CapDrop"),
            "security_opt": hc.get("SecurityOpt"),
            "memory": hc.get("Memory"),
            "nano_cpus": hc.get("NanoCpus"),
            "pids_limit": hc.get("PidsLimit"),
            "user": info["Config"].get("User"),
            "socket": self.socket_path(name).exists(),
        }

    async def logs(self, name: str, tail: int) -> str:
        info = await self.engine.inspect(self.container_name(name))
        if info is None:
            return ""
        return redact(await self.engine.logs(info["Id"], tail), self.secrets.get(name, set()))

    async def reap_idle(self) -> list[str]:
        now = time.monotonic()
        stopped = []
        for c in await self.engine.list_containers(f"{LABEL}={self.instance}"):
            name = c["Labels"].get("erag.mcp", "")
            last = self.last_used.get(name)
            if last is None or now - last > self.idle.get(name, self.settings.runner_idle_minutes) * 60:
                await self.engine.remove(c["Id"])
                self.last_used.pop(name, None)
                stopped.append(name)
        return stopped


def create_runner_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    s = settings or get_settings()
    eng = engine or DockerEngine(s.runner_docker_socket)
    runner = Runner(s, eng)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def reaper() -> None:
            while True:
                await asyncio.sleep(30)
                with suppress(EngineError):
                    await runner.reap_idle()

        task = asyncio.create_task(reaper())
        yield
        task.cancel()

    app = FastAPI(title="EnterpriseRAG MCP runner", lifespan=lifespan)
    app.state.runner = runner

    def auth(request: Request) -> None:
        token = s.runner_token.get_secret_value() if s.runner_token else ""
        sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "runner token not configured")
        if not hmac.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid runner token")

    @app.get("/v1/images", dependencies=[Depends(auth)])
    async def images() -> dict[str, list[str]]:
        return {"images": sorted(runner.images)}

    @app.post("/v1/servers/{name}/ensure", dependencies=[Depends(auth)])
    async def ensure(name: str, spec: Spec) -> dict[str, Any]:
        try:
            return await runner.ensure(name, spec)
        except EngineError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    @app.post("/v1/servers/{name}/stop", dependencies=[Depends(auth)])
    async def stop(name: str) -> dict[str, bool]:
        return {"stopped": await runner.stop(name)}

    @app.get("/v1/servers/{name}", dependencies=[Depends(auth)])
    async def get_status(name: str) -> dict[str, Any]:
        return await runner.status(name)

    @app.get("/v1/servers/{name}/logs", dependencies=[Depends(auth)])
    async def get_logs(name: str, tail: int = 200) -> dict[str, str]:
        return {"logs": await runner.logs(name, max(1, min(tail, 2000)))}

    return app


def main() -> None:
    import uvicorn

    listen = os.environ.get("ERAG_RUNNER_LISTEN", "127.0.0.1:8790")
    host, _, port = listen.rpartition(":")
    uvicorn.run(create_runner_app(), host=host, port=int(port), log_level="info")


if __name__ == "__main__":
    main()

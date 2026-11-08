"""App side of the isolated runner (ADR 0022): validate an MCP's runtime config and ask the runner
for a running container. The app never talks to the container engine itself."""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.mcp.gateway import Endpoint, HttpEndpoint
from app.models import McpServer
from app.services import vault

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# Tests can route the client to an in-process runner app (httpx.ASGITransport).
transport_override: httpx.AsyncBaseTransport | None = None


class RunnerError(RuntimeError):
    pass


class MountSpec(BaseModel):
    source: str = Field(max_length=500)
    target: str = Field(pattern=r"^/data(/[A-Za-z0-9._-]+)+$", max_length=200)


class RuntimeSpec(BaseModel):
    image: str = Field(min_length=1, max_length=100)
    cpus: float = Field(default=0.5, gt=0, le=8)
    memory_mb: int = Field(default=256, ge=32, le=16_384)
    pids: int = Field(default=64, ge=8, le=1024)
    idle_minutes: int = Field(default=15, ge=1, le=1440)
    env: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)  # ENV_VAR -> vault secret name
    mounts: list[MountSpec] = Field(default_factory=list, max_length=5)
    network: str = Field(default="none", pattern="^none$")


def validate_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = RuntimeSpec.model_validate(runtime)
    except ValidationError as exc:
        raise ValueError(f"invalid runtime: {exc.errors(include_url=False)[0]['msg']}") from exc
    for k in [*spec.env, *spec.secrets]:
        if not ENV_NAME.match(k):
            raise ValueError(f"invalid environment variable name {k!r}")
    overlap = set(spec.env) & set(spec.secrets)
    if overlap:
        raise ValueError(f"{sorted(overlap)} set both as plain and secret variables")
    return spec.model_dump()


class RunnerClient:
    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        if not s.runner_url or s.runner_token is None:
            raise RunnerError("the isolated runner is not configured (ERAG_RUNNER_URL, ERAG_RUNNER_TOKEN)")
        self.base_url = s.runner_url
        self.headers = {"Authorization": f"Bearer {s.runner_token.get_secret_value()}"}

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url, transport=transport_override, headers=self.headers, timeout=60
        ) as c:
            r = await c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise RunnerError(f"runner refused ({r.status_code}): {detail}")
        return dict(r.json())

    async def ensure(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/v1/servers/{name}/ensure", json=spec)

    async def stop(self, name: str) -> dict[str, Any]:
        return await self._call("POST", f"/v1/servers/{name}/stop")

    async def status(self, name: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/servers/{name}")

    async def logs(self, name: str, tail: int = 200) -> str:
        return str((await self._call("GET", f"/v1/servers/{name}/logs", params={"tail": tail}))["logs"])

    async def images(self) -> list[str]:
        return list((await self._call("GET", "/v1/images"))["images"])


async def runner_spec(db: AsyncSession, server: McpServer) -> dict[str, Any]:
    """The runner request for a server: its config plus exactly the secrets assigned to it (vault, audited)."""
    rt = RuntimeSpec.model_validate(server.runtime or {})
    secret_env = {
        env: await vault.reveal_assigned(db, name, consumer=f"mcp:{server.id}", assigned=list(rt.secrets.values()))
        for env, name in rt.secrets.items()
    }
    return {
        "image": rt.image,
        "cpus": rt.cpus,
        "memory_mb": rt.memory_mb,
        "pids": rt.pids,
        "idle_minutes": rt.idle_minutes,
        "env": rt.env,
        "secret_env": secret_env,
        "mounts": [m.model_dump() for m in rt.mounts],
        "network": "none",
    }


async def runner_endpoint(db: AsyncSession, server: McpServer, headers: dict[str, str], settings: Settings) -> Endpoint:
    """Start (or reuse) the server's container and return its Unix-socket endpoint."""
    info = await RunnerClient(settings).ensure(server.name, await runner_spec(db, server))
    return HttpEndpoint(url="http://localhost/mcp", headers=headers, uds=str(info["socket"]))

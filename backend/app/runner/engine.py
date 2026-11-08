"""Container engine behind a small interface (ADR 0022). Docker's Engine API over its Unix socket;
Podman exposes a compatible API, so a Podman socket can be used by pointing ERAG_RUNNER_DOCKER_SOCKET
at it (not verified)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import httpx

API = "/v1.41"  # oldest version with everything we use; Docker and Podman both serve it


class EngineError(RuntimeError):
    pass


class Engine(Protocol):
    async def image_id(self, ref: str) -> str | None: ...
    async def create(self, name: str, spec: dict[str, Any]) -> str: ...
    async def start(self, cid: str) -> None: ...
    async def stop(self, cid: str, grace_seconds: int = 5) -> None: ...
    async def remove(self, cid: str) -> None: ...
    async def inspect(self, name_or_id: str) -> dict[str, Any] | None: ...
    async def logs(self, cid: str, tail: int) -> str: ...
    async def list_containers(self, label: str) -> list[dict[str, Any]]: ...
    async def exec(self, cid: str, cmd: list[str]) -> tuple[int, str]: ...


class DockerEngine:
    def __init__(self, socket: Path) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket)), base_url="http://docker" + API, timeout=60
        )

    async def _ok(self, r: httpx.Response, *allowed: int) -> httpx.Response:
        if r.status_code >= 400 and r.status_code not in allowed:
            raise EngineError(f"engine HTTP {r.status_code}: {r.text[:300]}")
        return r

    async def image_id(self, ref: str) -> str | None:
        r = await self.client.get(f"/images/{ref}/json")
        if r.status_code == 404:
            return None
        await self._ok(r)
        return str(r.json()["Id"])

    async def create(self, name: str, spec: dict[str, Any]) -> str:
        r = await self._ok(await self.client.post("/containers/create", params={"name": name}, json=spec))
        return str(r.json()["Id"])

    async def start(self, cid: str) -> None:
        await self._ok(await self.client.post(f"/containers/{cid}/start"), 304)

    async def stop(self, cid: str, grace_seconds: int = 5) -> None:
        await self._ok(await self.client.post(f"/containers/{cid}/stop", params={"t": grace_seconds}), 304, 404)

    async def remove(self, cid: str) -> None:
        await self._ok(await self.client.delete(f"/containers/{cid}", params={"force": "true"}), 404)

    async def inspect(self, name_or_id: str) -> dict[str, Any] | None:
        r = await self.client.get(f"/containers/{name_or_id}/json")
        if r.status_code == 404:
            return None
        return dict((await self._ok(r)).json())

    async def logs(self, cid: str, tail: int) -> str:
        r = await self._ok(
            await self.client.get(f"/containers/{cid}/logs", params={"stdout": 1, "stderr": 1, "tail": tail})
        )
        return _demux(r.content)

    async def list_containers(self, label: str) -> list[dict[str, Any]]:
        r = await self._ok(
            await self.client.get("/containers/json", params={"all": 1, "filters": f'{{"label":["{label}"]}}'})
        )
        return list(r.json())

    async def exec(self, cid: str, cmd: list[str]) -> tuple[int, str]:
        """Used by tests to probe confinement from inside a container."""
        r = await self._ok(
            await self.client.post(
                f"/containers/{cid}/exec", json={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True}
            )
        )
        eid = r.json()["Id"]
        out = await self._ok(await self.client.post(f"/exec/{eid}/start", json={"Detach": False, "Tty": False}))
        info = (await self._ok(await self.client.get(f"/exec/{eid}/json"))).json()
        return int(info.get("ExitCode") or 0), _demux(out.content)

    async def aclose(self) -> None:
        await self.client.aclose()


def _demux(raw: bytes) -> str:
    """Docker multiplexed stream (8-byte frame headers) to text; plain text passes through."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1 : i + 4] == b"\x00\x00\x00":
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        out.append(raw[i + 8 : i + 8 + size])
        i += 8 + size
    if i == 0:
        return raw.decode(errors="replace")
    return b"".join(out).decode(errors="replace")

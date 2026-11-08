"""Client side of the isolated MCP runner (filled in by R5)."""

from __future__ import annotations

from app.core.config import Settings
from app.mcp.gateway import Endpoint
from app.models import McpServer


class RunnerError(RuntimeError):
    pass


async def runner_endpoint(server: McpServer, headers: dict[str, str], settings: Settings) -> Endpoint:
    raise RunnerError("the isolated runner is not configured")


def validate_runtime(runtime: dict[str, object]) -> dict[str, object]:
    raise ValueError("the isolated runner is not available in this release")

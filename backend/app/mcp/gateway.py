"""MCP gateway: the only component that talks to MCP servers (it is an MCP client).

Allowlist (registered + enabled), per-user/tool rate limit, signed permission context in
`_meta`, timeouts. Authorization of the call happens before (tool_authz); validation after.
"""

from __future__ import annotations

import json
import sys
from typing import Any, cast

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import TextContent
from mcp.types import Tool as McpTool
from mcp_types import RequestParamsMeta

from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.ratelimit import RateLimiter
from app.mcp.registry import ServerSpec
from mcp_servers.permission_context import META_KEY

_limiter: RateLimiter | None = None


class GatewayError(RuntimeError):
    pass


class RateLimited(GatewayError):
    pass


def limiter(settings: Settings) -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(settings.tool_rate_limit_per_minute)
    return _limiter


class McpGateway:
    def __init__(self, settings: Settings | None = None, timeout_seconds: float = 30.0) -> None:
        self.settings = settings or get_settings()
        self.timeout = timeout_seconds

    def _params(self, spec: ServerSpec) -> StdioServerParameters:
        s = self.settings
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", spec.module],
            cwd=str(REPO_ROOT),
            env={
                "ERAG_MCP_CONTEXT_SECRET": s.mcp_context_secret.get_secret_value(),
                "ERAG_MCP_FILES_ROOT": str(s.mcp_files_root),
                "ERAG_MCP_TICKETS_DB": str(s.mcp_tickets_db),
            },
        )

    async def list_tools(self, spec: ServerSpec) -> list[McpTool]:
        with anyio.fail_after(self.timeout):
            async with Client(self._params(spec)) as client:
                return list((await client.list_tools()).tools)

    def check_rate(self, user_id: str, tool_key: str) -> None:
        if not limiter(self.settings).allow(f"{user_id}:{tool_key}"):
            raise RateLimited("tool rate limit exceeded")

    async def call(self, spec: ServerSpec, tool: str, arguments: dict[str, Any], token: str) -> dict[str, Any]:
        """Call a tool with the permission context attached. Returns the decoded JSON result."""
        try:
            with anyio.fail_after(self.timeout):
                async with Client(self._params(spec)) as client:
                    result = await client.call_tool(tool, arguments, meta=cast(RequestParamsMeta, {META_KEY: token}))
        except TimeoutError as exc:
            raise GatewayError(f"{spec.name}.{tool} timed out") from exc
        if result.structured_content is not None:
            payload: Any = result.structured_content
        else:
            texts = [c.text for c in result.content if isinstance(c, TextContent)]
            raw = "\n".join(texts)
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"text": raw}
        if result.is_error:
            return {"error": payload if isinstance(payload, str) else json.dumps(payload)[:500]}
        if not isinstance(payload, dict):
            return {"result": payload}
        return cast(dict[str, Any], payload)

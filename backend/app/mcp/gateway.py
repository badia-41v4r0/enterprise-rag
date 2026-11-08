"""MCP gateway: the only component that talks to MCP servers (it is an MCP client).

Transports: stdio for the built-in reference servers, streamable HTTP for registered remote servers
(through the SSRF guard: public addresses only unless allowlisted, IP pinned, TLS verified), and
HTTP over a Unix socket for servers in the isolated runner (R5). Per-user/tool rate limit, signed
permission context in `_meta` where the server understands it, timeouts. Before a registered tool
is called, the server's current definition of that tool is compared with the approved hash in the
same session (rug-pull defence). Authorization happens before (tool_authz); validation after.
"""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, cast

import anyio
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from mcp.types import Tool as McpTool
from mcp_types import RequestParamsMeta

from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.netguard import PinnedTransport2
from app.core.ratelimit import RateLimiter
from app.mcp.registry import ServerSpec
from mcp_servers.permission_context import META_KEY

_limiter: RateLimiter | None = None
_tool_limiters: dict[int, RateLimiter] = {}


class GatewayError(RuntimeError):
    pass


class RateLimited(GatewayError):
    pass


class ToolChanged(GatewayError):
    """The server's definition of a tool no longer matches what the admin approved."""

    def __init__(self, current: McpTool | None) -> None:
        super().__init__("tool definition changed since approval" if current else "tool no longer offered by server")
        self.current = current


@dataclass(frozen=True)
class StdioEndpoint:
    module: str


@dataclass(frozen=True)
class HttpEndpoint:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    # Unix socket of a runner-managed container (no network, no SSRF surface); else a guarded URL.
    uds: str | None = None
    allowlist: tuple[str, ...] = ()


Endpoint = StdioEndpoint | HttpEndpoint


def tool_hash(name: str, description: str | None, input_schema: dict[str, Any] | None) -> str:
    blob = json.dumps({"n": name, "d": description or "", "s": input_schema or {}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def limiter(settings: Settings) -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(settings.tool_rate_limit_per_minute)
    return _limiter


def tool_limiter(per_minute: int) -> RateLimiter:
    if per_minute not in _tool_limiters:
        _tool_limiters[per_minute] = RateLimiter(per_minute)
    return _tool_limiters[per_minute]


class McpGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        timeout_seconds: float = 30.0,
        overrides: dict[str, Endpoint] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.timeout = timeout_seconds
        # Lets tests (and deployments) reach a built-in reference server over HTTP instead of stdio.
        self.overrides = overrides or {}

    def endpoint(self, target: ServerSpec | Endpoint) -> Endpoint:
        if isinstance(target, ServerSpec):
            return self.overrides.get(target.name) or StdioEndpoint(target.module)
        return target

    def _stdio(self, ep: StdioEndpoint) -> StdioServerParameters:
        s = self.settings
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", ep.module],
            cwd=str(REPO_ROOT),
            env={
                "ERAG_MCP_CONTEXT_SECRET": s.mcp_context_secret.get_secret_value(),
                "ERAG_MCP_FILES_ROOT": str(s.mcp_files_root),
                "ERAG_MCP_TICKETS_DB": str(s.mcp_tickets_db),
            },
        )

    async def _open(self, stack: AsyncExitStack, ep: Endpoint, http_timeout: float) -> Client:
        if isinstance(ep, StdioEndpoint):
            return await stack.enter_async_context(Client(self._stdio(ep)))
        transport: httpx2.AsyncBaseTransport = (
            httpx2.AsyncHTTPTransport(uds=ep.uds) if ep.uds else PinnedTransport2(ep.allowlist)
        )
        http = await stack.enter_async_context(
            httpx2.AsyncClient(
                transport=transport, headers=ep.headers, timeout=httpx2.Timeout(http_timeout), follow_redirects=False
            )
        )
        return await stack.enter_async_context(Client(streamable_http_client(ep.url, http_client=http)))

    async def list_tools(self, target: ServerSpec | Endpoint, time_limit: float | None = None) -> list[McpTool]:
        t = time_limit or self.timeout
        try:
            with anyio.fail_after(t):
                async with AsyncExitStack() as stack:
                    client = await self._open(stack, self.endpoint(target), t)
                    return list((await client.list_tools()).tools)
        except TimeoutError as exc:
            raise GatewayError("server did not answer in time") from exc

    def check_rate(self, user_id: str, tool_key: str, per_tool_per_minute: int | None = None) -> None:
        if not limiter(self.settings).allow(f"{user_id}:{tool_key}"):
            raise RateLimited("tool rate limit exceeded")
        if per_tool_per_minute and not tool_limiter(per_tool_per_minute).allow(tool_key):
            raise RateLimited("tool rate limit exceeded (all users)")

    async def call(
        self,
        target: ServerSpec | Endpoint,
        tool: str,
        arguments: dict[str, Any],
        token: str | None,
        *,
        expected_hash: str | None = None,
        time_limit: float | None = None,
    ) -> dict[str, Any]:
        """Call a tool (with the permission context attached when given). Returns the decoded JSON result."""
        t = time_limit or self.timeout
        changed: list[McpTool | None] = []
        result = None
        try:
            with anyio.fail_after(t):
                async with AsyncExitStack() as stack:
                    client = await self._open(stack, self.endpoint(target), t)
                    if expected_hash is not None:
                        current = next((x for x in (await client.list_tools()).tools if x.name == tool), None)
                        if current is None or tool_hash(current.name, current.description, current.input_schema) != (
                            expected_hash
                        ):
                            # Raised after the session is closed (inside it, the SDK wraps it in a group).
                            changed.append(current)
                    if not changed:
                        meta = cast(RequestParamsMeta, {META_KEY: token}) if token else None
                        result = await client.call_tool(tool, arguments, meta=meta)
        except TimeoutError as exc:
            raise GatewayError(f"{tool} timed out") from exc
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(f"{tool} failed: {type(exc).__name__}") from exc
        if changed:
            raise ToolChanged(changed[0])
        assert result is not None
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

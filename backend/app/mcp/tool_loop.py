"""Tool runtime used by the chat orchestrator: visible tools, and the guarded execution path.

execute(): resolve -> parse -> tool_authz (grant, schema, resource) -> rate limit ->
signed permission context (scoped from Postgres) -> gateway -> result validator -> audit.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, can_read_document
from app.core.config import Settings, get_settings
from app.llm.providers.base import ToolCall, ToolSpec
from app.mcp.gateway import GatewayError, McpGateway
from app.mcp.registry import SERVERS, ServerSpec, model_tool_name, split_model_tool_name
from app.mcp.tool_authz import ToolDenied, authorize
from app.mcp.validator import validate_result
from app.models import Classification, Container, Document, McpServer, Tool, User
from app.orchestrator.chat import ToolOutcome
from app.services.audit import audit
from mcp_servers.permission_context import mint


@dataclass
class Scope:
    containers: dict[str, str]  # readable slug -> classification
    excluded_paths: set[str]  # restricted files in readable containers that the user cannot read
    departments: list[str]

    def for_server(self, spec: ServerSpec) -> dict[str, Any]:
        if spec.scope_kind == "containers":
            return {"containers": self.containers, "exclude": sorted(self.excluded_paths)}
        return {"departments": self.departments}


async def compute_scope(db: AsyncSession, perms: EffectivePermissions) -> Scope:
    """Derive tool data scope from the same container permissions used for retrieval."""
    containers: dict[uuid.UUID, Container] = {}
    if perms.readable_containers:
        containers = {
            c.id: c
            for c in (await db.scalars(select(Container).where(Container.id.in_(perms.readable_containers)))).all()
        }
    excluded: set[str] = set()
    if containers:
        restricted = (
            await db.scalars(
                select(Document).where(Document.container_id.in_(containers.keys()), Document.restricted.is_(True))
            )
        ).all()
        for d in restricted:
            if not can_read_document(perms, container_id=d.container_id, document_id=d.id, restricted=True):
                excluded.add(f"{containers[d.container_id].slug}/{d.filename}")
    slugs = {c.slug: c.classification.value for c in containers.values()}
    departments = sorted(
        {dept for spec in SERVERS.values() for dept, slug in spec.department_containers.items() if slug in slugs}
    )
    return Scope(slugs, excluded, departments)


class McpToolRuntime:
    def __init__(self, gateway: McpGateway | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or McpGateway(self.settings)

    async def _tools(self, db: AsyncSession, perms: EffectivePermissions) -> list[tuple[McpServer, Tool]]:
        if not perms.executable_tools:
            return []
        rows = (
            await db.execute(
                select(McpServer, Tool)
                .join(Tool, Tool.server_id == McpServer.id)
                .where(Tool.id.in_(perms.executable_tools), Tool.enabled.is_(True), McpServer.enabled.is_(True))
                .order_by(McpServer.name, Tool.name)
            )
        ).all()
        return [(s, t) for s, t in rows if s.name in SERVERS]

    async def visible_tools(self, db: AsyncSession, perms: EffectivePermissions) -> list[ToolSpec]:
        """Only tools the user may execute are ever shown to the model."""
        return [
            ToolSpec(
                name=model_tool_name(s.name, t.name),
                description=t.description,
                parameters=t.input_schema or {"type": "object", "properties": {}},
            )
            for s, t in await self._tools(db, perms)
        ]

    async def _resolve(self, db: AsyncSession, name: str) -> tuple[McpServer, Tool] | None:
        parts = split_model_tool_name(name)
        if parts is None:
            return None
        row = (
            await db.execute(
                select(McpServer, Tool)
                .join(Tool, Tool.server_id == McpServer.id)
                .where(McpServer.name == parts[0], Tool.name == parts[1])
            )
        ).first()
        return (row[0], row[1]) if row else None

    def _deny(
        self, db: AsyncSession, user: User, call: ToolCall, reason: str, extra: dict[str, Any] | None = None
    ) -> ToolOutcome:
        audit(
            db,
            "tool.call",
            user_id=user.id,
            resource=f"tool:{call.name}",
            decision="deny",
            details={"reason": reason, "arguments": call.arguments[:1000], **(extra or {})},
        )
        return ToolOutcome(
            content=json.dumps({"error": f"Tool call refused: {reason}"}),
            classification=Classification.public,
            ok=False,
            summary={"refused": reason},
        )

    async def execute(self, db: AsyncSession, user: User, perms: EffectivePermissions, call: ToolCall) -> ToolOutcome:
        resolved = await self._resolve(db, call.name)
        if resolved is None or resolved[1].id not in perms.executable_tools:
            # Same answer for "does not exist" and "not yours": hidden tools stay hidden.
            return self._deny(db, user, call, "unknown tool")
        server, tool = resolved
        spec = SERVERS[server.name]
        try:
            args = call.parsed_arguments()
        except (ValueError, json.JSONDecodeError):
            return self._deny(db, user, call, "arguments are not a JSON object")

        scope = await compute_scope(db, perms)
        try:
            authorize(
                perms, server, tool, args, readable_slugs=set(scope.containers), excluded_paths=scope.excluded_paths
            )
            self.gateway.check_rate(str(user.id), call.name)
        except ToolDenied as exc:
            return self._deny(db, user, call, exc.reason)
        except GatewayError as exc:
            return self._deny(db, user, call, str(exc))

        token = mint(
            self.settings.mcp_context_secret.get_secret_value(),
            server=server.name,
            tool=tool.name,
            user_id=str(user.id),
            scopes=scope.for_server(spec),
            ttl_seconds=self.settings.mcp_token_ttl_seconds,
        )
        try:
            raw = await self.gateway.call(spec, tool.name, args, token)
        except GatewayError as exc:
            return self._deny(db, user, call, str(exc))

        result = validate_result(raw, tool.classification, self.settings.tool_result_max_bytes)
        audit(
            db,
            "tool.call",
            user_id=user.id,
            resource=f"tool:{call.name}",
            decision="allow" if result.ok else "error",
            details={
                "arguments": args,
                "classification": result.classification.value,
                "flags": result.flags,
                "bytes": result.size_bytes,
                "scope": scope.for_server(spec),
            },
        )
        return ToolOutcome(
            content=result.content,
            classification=result.classification,
            ok=result.ok,
            summary={"classification": result.classification.value, "flags": result.flags},
        )

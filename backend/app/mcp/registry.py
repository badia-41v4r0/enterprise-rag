"""Registry of MCP servers and their tools (persisted in `mcp_servers` / `tools`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Classification, McpServer, Tool
from app.services.audit import audit


@dataclass(frozen=True)
class ServerSpec:
    name: str
    module: str  # run as `python -m <module>` over stdio
    # Declared minimum classification of each tool's results (the validator may raise it).
    tool_classification: dict[str, Classification]
    scope_kind: Literal["containers", "departments"]
    # departments -> container slug whose read permission grants that department's records
    department_containers: dict[str, str] = field(default_factory=dict)


SERVERS: dict[str, ServerSpec] = {
    "files": ServerSpec(
        name="files",
        module="mcp_servers.files_server",
        tool_classification={"list_files": Classification.public, "read_file": Classification.public},
        scope_kind="containers",
    ),
    "tickets": ServerSpec(
        name="tickets",
        module="mcp_servers.tickets_server",
        tool_classification={"search_tickets": Classification.internal, "list_invoices": Classification.confidential},
        scope_kind="departments",
        department_containers={"hr": "hr-confidential", "finance": "finance", "engineering": "engineering"},
    ),
}


def model_tool_name(server: str, tool: str) -> str:
    """Function names exposed to models must match ^[a-zA-Z0-9_-]+$."""
    return f"{server}__{tool}"


def split_model_tool_name(name: str) -> tuple[str, str] | None:
    server, sep, tool = name.partition("__")
    return (server, tool) if sep and server and tool else None


async def sync_registry(db: AsyncSession) -> int:
    """Start each reference server, list its tools and upsert them. Returns the number of tools."""
    from app.mcp.gateway import McpGateway

    gateway = McpGateway()
    count = 0
    for spec in SERVERS.values():
        server = await db.scalar(select(McpServer).where(McpServer.name == spec.name))
        if server is None:
            server = McpServer(name=spec.name, command=spec.module, enabled=True)
            db.add(server)
            await db.flush()
        server.command = spec.module
        for t in await gateway.list_tools(spec):
            tool = await db.scalar(select(Tool).where(Tool.server_id == server.id, Tool.name == t.name))
            declared = spec.tool_classification.get(t.name, Classification.confidential)
            if tool is None:
                tool = Tool(server_id=server.id, name=t.name, classification=declared)
                db.add(tool)
            tool.description = t.description or ""
            tool.input_schema = dict(t.input_schema or {"type": "object", "properties": {}})
            # Never lower a classification an admin raised; raise it to the declared floor.
            tool.classification = Classification.max(tool.classification, declared)
            count += 1
    audit(db, "mcp.registry.sync", details={"tools": count})
    await db.commit()
    return count

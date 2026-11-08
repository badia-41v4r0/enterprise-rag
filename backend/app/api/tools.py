"""MCP tools: user-visible list, and admin management (registry sync, enable/disable, classification)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth, require_admin
from app.core.db import get_db
from app.mcp.registry import model_tool_name, sync_registry
from app.mcp.tool_loop import McpToolRuntime
from app.models import Classification, McpServer, Tool
from app.services.audit import audit

router = APIRouter(prefix="/api", tags=["tools"])


class ToolOut(BaseModel):
    id: uuid.UUID
    server: str
    name: str
    model_name: str
    description: str
    classification: Classification
    enabled: bool
    server_enabled: bool
    input_schema: dict[str, Any]


class ToolPatch(BaseModel):
    enabled: bool | None = None
    classification: Classification | None = None


class ServerPatch(BaseModel):
    enabled: bool


class VisibleToolOut(BaseModel):
    name: str
    description: str


@router.get("/tools")
async def my_tools(auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> list[VisibleToolOut]:
    """Exactly the tool list the model would be offered for this user."""
    return [
        VisibleToolOut(name=t.name, description=t.description)
        for t in await McpToolRuntime().visible_tools(db, auth.perms)
    ]


@router.get("/admin/tools")
async def list_tools(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[ToolOut]:
    rows = (
        await db.execute(
            select(McpServer, Tool).join(Tool, Tool.server_id == McpServer.id).order_by(McpServer.name, Tool.name)
        )
    ).all()
    return [
        ToolOut(
            id=t.id,
            server=s.name,
            name=t.name,
            model_name=model_tool_name(s.name, t.name),
            description=t.description,
            classification=t.classification,
            enabled=t.enabled,
            server_enabled=s.enabled,
            input_schema=t.input_schema,
        )
        for s, t in rows
    ]


@router.post("/admin/tools/sync")
async def sync_tools(auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    n = await sync_registry(db)
    audit(db, "admin.tools.sync", user_id=auth.user.id, details={"tools": n})
    await db.commit()
    return {"tools": n}


@router.patch("/admin/tools/{tool_id}")
async def patch_tool(
    tool_id: uuid.UUID, body: ToolPatch, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    tool = await db.get(Tool, tool_id)
    if tool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(tool, k, v)
    audit(db, "admin.tool.update", user_id=auth.user.id, resource=f"tool:{tool_id}", details=changes)
    await db.commit()
    return {"status": "ok"}


@router.patch("/admin/mcp-servers/{name}")
async def patch_server(
    name: str, body: ServerPatch, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    server = await db.scalar(select(McpServer).where(McpServer.name == name))
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    server.enabled = body.enabled
    audit(
        db,
        "admin.mcp_server.update",
        user_id=auth.user.id,
        resource=f"mcp_server:{name}",
        details={"enabled": body.enabled},
    )
    await db.commit()
    return {"status": "ok"}

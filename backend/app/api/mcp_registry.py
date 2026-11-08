"""MCP registry (admin): register remote servers, review discovered tools, clearance (ADR 0021)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_admin, require_step_up
from app.core.config import get_settings
from app.core.db import get_db
from app.core.netguard import BlockedDestination, check_url
from app.mcp.gateway import GatewayError
from app.mcp.registry import model_tool_name
from app.mcp.review import SERVER_NAME, discover, enable_problem, sanitize_text, validate_constraints
from app.models import Classification, Grant, McpServer, ResourceType, ServerStatus, Tool
from app.services import vault
from app.services.audit import audit

router = APIRouter(prefix="/api/admin/registry", tags=["mcp-registry"])
DEFAULT_LIMITS = {"timeout_seconds": 30, "rate_limit_per_minute": 30, "max_result_bytes": 16_000}


class Limits(BaseModel):
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=10_000)
    max_result_bytes: int = Field(default=16_000, ge=256, le=1_000_000)


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=31)
    transport: str = Field(default="http", pattern="^(http|runner)$")
    url: str | None = Field(default=None, max_length=1000)
    description: str = Field(default="", max_length=2000)
    auth_secret: str | None = None
    # Highest classification that may be sent to it. Default: public (nothing internal leaves).
    max_classification: Classification = Classification.public
    # Label on everything it returns. Default: confidential (rest of the conversation stays local).
    result_classification: Classification = Classification.confidential
    send_permission_context: bool = False
    limits: Limits = Field(default_factory=Limits)
    runtime: dict[str, Any] = Field(default_factory=dict)


class ServerPatch(BaseModel):
    status: ServerStatus | None = None
    url: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=2000)
    auth_secret: str | None = None
    max_classification: Classification | None = None
    result_classification: Classification | None = None
    send_permission_context: bool | None = None
    limits: Limits | None = None
    runtime: dict[str, Any] | None = None


class ToolReviewPatch(BaseModel):
    enabled: bool | None = None
    classification: Classification | None = None
    arg_constraints: dict[str, Any] | None = None


class ToolView(BaseModel):
    id: uuid.UUID
    name: str
    model_name: str
    description: str
    description_for_model: str
    input_schema: dict[str, Any]
    classification: Classification
    enabled: bool
    needs_review: bool
    review_reason: str | None
    approved: bool
    arg_constraints: dict[str, Any]
    cannot_enable: str | None


class ServerView(BaseModel):
    id: uuid.UUID
    name: str
    builtin: bool
    transport: str
    url: str | None
    description: str
    status: ServerStatus
    auth_secret: str | None
    max_classification: Classification
    result_classification: Classification
    send_permission_context: bool
    limits: dict[str, Any]
    runtime: dict[str, Any]
    last_discovered_at: datetime | None
    tools: list[ToolView]


@vault.register_reference_finder
async def _mcp_refs(db: AsyncSession) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in (await db.scalars(select(McpServer).where(McpServer.builtin.is_(False)))).all():
        if s.auth_secret:
            out.setdefault(s.auth_secret, []).append(f"MCP server: {s.name}")
        for name in (s.runtime or {}).get("secrets", {}).values():
            out.setdefault(str(name), []).append(f"MCP server: {s.name} (runner)")
    return out


def _tool_view(s: McpServer, t: Tool) -> ToolView:
    return ToolView(
        id=t.id,
        name=t.name,
        model_name=model_tool_name(s.name, t.name),
        description=t.description,
        description_for_model=t.description if s.builtin else sanitize_text(t.description)[0],
        input_schema=t.input_schema,
        classification=t.classification,
        enabled=t.enabled,
        needs_review=t.needs_review,
        review_reason=t.review_reason,
        approved=s.builtin or (t.approved_hash is not None and t.approved_hash == t.content_hash),
        arg_constraints=t.arg_constraints or {},
        cannot_enable=None if s.builtin else enable_problem(s, t),
    )


async def _view(db: AsyncSession, s: McpServer) -> ServerView:
    tools = (await db.scalars(select(Tool).where(Tool.server_id == s.id).order_by(Tool.name))).all()
    return ServerView(
        id=s.id,
        name=s.name,
        builtin=s.builtin,
        transport=s.transport,
        url=s.url,
        description=s.description,
        status=s.status,
        auth_secret=s.auth_secret,
        max_classification=s.max_classification,
        result_classification=s.result_classification,
        send_permission_context=s.send_permission_context,
        limits=s.limits or {},
        runtime=s.runtime or {},
        last_discovered_at=s.last_discovered_at,
        tools=[_tool_view(s, t) for t in tools],
    )


async def _server_or_404(db: AsyncSession, server_id: uuid.UUID) -> McpServer:
    s = await db.get(McpServer, server_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Server not found")
    return s


async def _check_url(url: str | None) -> None:
    if not url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "an http server needs a URL")
    try:
        await check_url(url, get_settings().ssrf_allowlist)
    except BlockedDestination as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


async def _check_secret(db: AsyncSession, name: str | None) -> None:
    if name and not await vault.exists(db, name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"secret {name!r} does not exist")


async def _discover(db: AsyncSession, s: McpServer, user_id: uuid.UUID) -> dict[str, list[str]]:
    try:
        d = await discover(db, s, user_id=user_id)
    except (GatewayError, BlockedDestination, ValueError, RuntimeError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"tool discovery failed: {exc}") from exc
    return {"new": d.new, "changed": d.changed, "removed": d.removed, "unchanged": d.unchanged}


@router.get("/servers")
async def list_servers(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[ServerView]:
    return [await _view(db, s) for s in (await db.scalars(select(McpServer).order_by(McpServer.name))).all()]


@router.post("/servers", status_code=201)
async def register_server(
    body: RegisterIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> ServerView:
    """Register a server as `pending`, then call tools/list. Every discovered tool starts disabled."""
    if not SERVER_NAME.match(body.name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name: lowercase letters, digits and '-'")
    if body.transport == "http":
        await _check_url(body.url)
    else:
        from app.runner.client import validate_runtime

        try:
            body.runtime = validate_runtime(body.runtime)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await _check_secret(db, body.auth_secret)
    s = McpServer(
        name=body.name,
        command="",
        builtin=False,
        transport=body.transport,
        url=body.url if body.transport == "http" else None,
        description=body.description,
        status=ServerStatus.pending,
        auth_secret=body.auth_secret,
        max_classification=body.max_classification,
        result_classification=body.result_classification,
        send_permission_context=body.send_permission_context,
        limits=body.limits.model_dump(),
        runtime=body.runtime,
        enabled=True,
        created_by=auth.user.id,
    )
    db.add(s)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "A server with this name exists") from exc
    audit(
        db,
        "admin.mcp.register",
        user_id=auth.user.id,
        resource=f"mcp_server:{s.name}",
        details=body.model_dump(mode="json"),
    )
    found = await _discover(db, s, auth.user.id)
    if not found["new"]:
        await db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the server offers no tools")
    await db.commit()
    return await _view(db, s)


@router.patch("/servers/{server_id}")
async def update_server(
    server_id: uuid.UUID,
    body: ServerPatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> ServerView:
    s = await _server_or_404(db, server_id)
    changes = body.model_dump(exclude_none=True, mode="json")
    if s.builtin and set(changes) - {"status", "max_classification"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "built-in servers only accept status and clearance")
    if body.url is not None:
        await _check_url(body.url)
        s.url = body.url
    if body.auth_secret is not None:
        await _check_secret(db, body.auth_secret or None)
        s.auth_secret = body.auth_secret or None
    if body.runtime is not None:
        from app.runner.client import validate_runtime

        try:
            s.runtime = validate_runtime(body.runtime)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    for k in ("status", "description", "max_classification", "result_classification", "send_permission_context"):
        v = getattr(body, k)
        if v is not None:
            setattr(s, k, v)
    if body.limits is not None:
        s.limits = body.limits.model_dump()
    if body.status is not None:
        s.enabled = body.status != ServerStatus.disabled
    audit(db, "admin.mcp.update", user_id=auth.user.id, resource=f"mcp_server:{s.name}", details=changes)
    await db.commit()
    return await _view(db, s)


@router.post("/servers/{server_id}/discover")
async def rediscover(
    server_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, list[str]]:
    """Re-run tools/list: new tools start disabled, changed or removed approved tools are disabled."""
    s = await _server_or_404(db, server_id)
    if s.builtin:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "use the tool sync for built-in servers")
    found = await _discover(db, s, auth.user.id)
    await db.commit()
    return found


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> None:
    s = await _server_or_404(db, server_id)
    if s.builtin:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "built-in servers cannot be deleted; disable them")
    tool_ids = list((await db.scalars(select(Tool.id).where(Tool.server_id == s.id))).all())
    if tool_ids:
        await db.execute(delete(Grant).where(Grant.resource_type == ResourceType.tool, Grant.resource_id.in_(tool_ids)))
    await db.delete(s)
    audit(db, "admin.mcp.delete", user_id=auth.user.id, resource=f"mcp_server:{s.name}")
    await db.commit()


@router.patch("/tools/{tool_id}")
async def review_tool(
    tool_id: uuid.UUID,
    body: ToolReviewPatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> ToolView:
    """Enable (approve the current definition), set the result label, set argument constraints."""
    t = await db.get(Tool, tool_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool not found")
    s = await db.get(McpServer, t.server_id)
    assert s is not None
    details: dict[str, Any] = body.model_dump(exclude_none=True, mode="json")
    if body.arg_constraints is not None:
        try:
            t.arg_constraints = validate_constraints(t, body.arg_constraints)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if body.classification is not None:
        t.classification = body.classification
    if body.enabled is True:
        problem = None if s.builtin else enable_problem(s, t)
        if problem:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
        t.enabled = True
        if not s.builtin:
            t.approved_hash, t.needs_review, t.review_reason = t.content_hash, False, None
            details["approved_hash"] = t.content_hash
    elif body.enabled is False:
        t.enabled = False
    audit(db, "admin.mcp.tool_review", user_id=auth.user.id, resource=f"tool:{s.name}__{t.name}", details=details)
    await db.commit()
    return _tool_view(s, t)

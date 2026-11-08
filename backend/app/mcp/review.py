"""Registered (non built-in) MCP servers: discovery, review state and constraints (ADR 0021).

Tools are discovered, not trusted: every new tool starts disabled; a change of name, description or
schema after approval disables the tool and flags it for review; descriptions are untrusted text,
bounded and sanitized before a model sees them.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.mcp.gateway import Endpoint, HttpEndpoint, McpGateway, tool_hash
from app.mcp.validator import _INJECTION
from app.models import McpServer, Tool
from app.services import vault
from app.services.audit import audit

MAX_DESCRIPTION = 1000
MAX_SCHEMA_BYTES = 16_000
SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
WITHHELD = "[description withheld: it contained instruction-like text]"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁠-⁩﻿]")
_MARKUP = re.compile(r"<[^>]{0,300}>")
CONSTRAINT_KEYS = {"enum", "pattern", "minimum", "maximum", "max_length", "force"}


def sanitize_text(text: str | None, limit: int = MAX_DESCRIPTION) -> tuple[str, list[str]]:
    flags: list[str] = []
    t = _MARKUP.sub("", _CONTROL.sub("", text or ""))
    t = " ".join(t.split())
    if len(t) > limit:
        t, flags = t[:limit] + "...", ["truncated"]
    if _INJECTION.search(t):
        return WITHHELD, [*flags, "prompt_injection"]
    return t, flags


def sanitize_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    flags: list[str] = []

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k in ("description", "title") and isinstance(x, str):
                    clean, f = sanitize_text(x, 300)
                    flags.extend(f)
                    out[k] = clean
                else:
                    out[k] = walk(x)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(copy.deepcopy(schema or {"type": "object", "properties": {}})), flags


def model_schema(tool: Tool) -> dict[str, Any]:
    """What the model is offered: sanitized, forced arguments removed, constraints shown as hints."""
    schema, _ = sanitize_schema(tool.input_schema)
    props: dict[str, Any] = dict(schema.get("properties") or {})
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    for name, c in (tool.arg_constraints or {}).items():
        if "force" in c:
            props.pop(name, None)
            required = [r for r in required if r != name]
        elif name in props and isinstance(props[name], dict):
            p = dict(props[name])
            for key, target in (
                ("enum", "enum"),
                ("pattern", "pattern"),
                ("minimum", "minimum"),
                ("maximum", "maximum"),
                ("max_length", "maxLength"),
            ):
                if key in c:
                    p[target] = c[key]
            props[name] = p
    schema["properties"] = props
    schema["required"] = required
    schema["type"] = "object"
    return schema


def enable_problem(server: McpServer, tool: Tool) -> str | None:
    """Why this tool cannot be enabled (None if it can)."""
    if not TOOL_NAME.match(tool.name) or len(f"{server.name}__{tool.name}") > 64:
        return "tool name is not usable as a function name"
    if len(json.dumps(tool.input_schema or {})) > MAX_SCHEMA_BYTES:
        return f"input schema larger than {MAX_SCHEMA_BYTES} bytes"
    if tool.review_reason == "no longer offered by the server":
        return "the server no longer offers this tool"
    return None


def validate_constraints(tool: Tool, constraints: dict[str, Any]) -> dict[str, Any]:
    props = (tool.input_schema or {}).get("properties") or {}
    clean: dict[str, Any] = {}
    for name, c in constraints.items():
        if name not in props:
            raise ValueError(f"{name!r} is not an argument of {tool.name}")
        if not isinstance(c, dict) or not c or set(c) - CONSTRAINT_KEYS:
            raise ValueError(f"constraint keys must be among {sorted(CONSTRAINT_KEYS)}")
        if "pattern" in c:
            if not isinstance(c["pattern"], str) or len(c["pattern"]) > 200:
                raise ValueError("pattern must be a string of at most 200 characters")
            re.compile(c["pattern"])
        if "enum" in c and (not isinstance(c["enum"], list) or not c["enum"] or len(c["enum"]) > 100):
            raise ValueError("enum must be a non-empty list (max 100)")
        for k in ("minimum", "maximum", "max_length"):
            if k in c and not isinstance(c[k], int | float):
                raise ValueError(f"{k} must be a number")
        clean[name] = c
    return clean


async def endpoint_for(db: AsyncSession, server: McpServer, settings: Settings | None = None) -> Endpoint:
    """HTTP endpoint of a registered server, with its auth secret from the vault (audited)."""
    s = settings or get_settings()
    headers: dict[str, str] = {}
    if server.auth_secret:
        token = await vault.reveal_assigned(
            db, server.auth_secret, consumer=f"mcp:{server.id}", assigned=[server.auth_secret]
        )
        headers["Authorization"] = f"Bearer {token}"
    if server.transport == "runner":
        from app.runner.client import runner_endpoint

        return await runner_endpoint(db, server, headers, s)
    if not server.url:
        raise ValueError("server has no URL")  # pragma: no cover - checked at registration
    return HttpEndpoint(url=server.url, headers=headers, allowlist=tuple(s.ssrf_allowlist))


@dataclass
class Discovery:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


async def discover(
    db: AsyncSession, server: McpServer, gateway: McpGateway | None = None, *, user_id: Any = None
) -> Discovery:
    """`tools/list` on a registered server and reconcile with what is stored. The caller commits."""
    gw = gateway or McpGateway()
    endpoint = await endpoint_for(db, server)
    live = await gw.list_tools(endpoint, time_limit=float(server.limits.get("timeout_seconds", 30)))
    existing = {t.name: t for t in (await db.scalars(select(Tool).where(Tool.server_id == server.id))).all()}
    result = Discovery()
    for lt in live:
        h = tool_hash(lt.name, lt.description, dict(lt.input_schema or {}))
        t = existing.pop(lt.name, None)
        if t is None:
            t = Tool(
                server_id=server.id,
                name=lt.name,
                description=lt.description or "",
                input_schema=dict(lt.input_schema or {"type": "object", "properties": {}}),
                classification=server.result_classification,
                enabled=False,
                content_hash=h,
            )
            db.add(t)
            result.new.append(lt.name)
        elif t.content_hash != h:
            t.description, t.input_schema, t.content_hash = lt.description or "", dict(lt.input_schema or {}), h
            if t.approved_hash and t.approved_hash != h:
                t.enabled, t.needs_review, t.review_reason = False, True, "definition changed since approval"
            result.changed.append(lt.name)
        else:
            if t.review_reason == "no longer offered by the server":
                t.review_reason = None
            result.unchanged.append(lt.name)
        _, flags = sanitize_text(lt.description)
        _, sflags = sanitize_schema(dict(lt.input_schema or {}))
        if "prompt_injection" in flags + sflags:
            t.needs_review = True
            t.review_reason = "description contains instruction-like text (withheld from models)"
    for name, t in existing.items():
        t.enabled, t.needs_review, t.review_reason = False, True, "no longer offered by the server"
        result.removed.append(name)
    server.last_discovered_at = datetime.now(UTC)
    audit(
        db,
        "mcp.discover",
        user_id=user_id,
        resource=f"mcp_server:{server.name}",
        details={"new": result.new, "changed": result.changed, "removed": result.removed},
    )
    return result

"""Tool authorization: user + tool + arguments + resource, decided before the gateway is called."""

from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.authz.policy import EffectivePermissions
from app.models import Classification, McpServer, ServerStatus, Tool


class ToolDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema as enforced: no extra properties, whatever the server declared."""
    s = copy.deepcopy(schema) or {"type": "object", "properties": {}}
    s.setdefault("type", "object")
    s["additionalProperties"] = False
    return s


def validate_arguments(tool: Tool, args: dict[str, Any]) -> None:
    if any(k.startswith("_") for k in args):
        raise ToolDenied("reserved argument names are not allowed")
    try:
        errors = sorted(Draft202012Validator(strict_schema(tool.input_schema)).iter_errors(args), key=str)
    except SchemaError as exc:  # pragma: no cover - registry stores server schemas
        raise ToolDenied("tool schema is invalid") from exc
    if errors:
        raise ToolDenied(f"invalid arguments: {errors[0].message}")


def apply_constraints(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Admin argument constraints (ADR 0021): forced values replace whatever the model sent; enum,
    pattern, bounds and length are enforced. Returns the arguments actually sent to the server."""
    out = dict(args)
    for name, c in (tool.arg_constraints or {}).items():
        if "force" in c:
            out[name] = c["force"]
            continue
        if name not in out:
            continue
        v = out[name]
        if "enum" in c and v not in c["enum"]:
            raise ToolDenied(f"argument {name!r} must be one of {c['enum']}")
        if "pattern" in c and (not isinstance(v, str) or not re.fullmatch(str(c["pattern"]), v)):
            raise ToolDenied(f"argument {name!r} does not match the allowed pattern")
        is_number = isinstance(v, int | float) and not isinstance(v, bool)
        if "minimum" in c and (not is_number or v < c["minimum"]):
            raise ToolDenied(f"argument {name!r} is below the allowed minimum")
        if "maximum" in c and (not is_number or v > c["maximum"]):
            raise ToolDenied(f"argument {name!r} is above the allowed maximum")
        if "max_length" in c and isinstance(v, str) and len(v) > c["max_length"]:
            raise ToolDenied(f"argument {name!r} is too long")
    return out


def check_clearance(server: McpServer, classification: Classification) -> None:
    """Filter before send: an MCP server is an external recipient, like a cloud model (ADR 0021)."""
    if classification.rank > server.max_classification.rank:
        raise ToolDenied(
            f"tool unavailable: this conversation contains {classification.value} data and the "
            f"{server.name} server is cleared only up to {server.max_classification.value}"
        )


def check_resources(
    server: McpServer, tool: Tool, args: dict[str, Any], readable_slugs: set[str], excluded_paths: set[str]
) -> None:
    """Tool-specific resource checks (first barrier; the server enforces the same scope again)."""
    if server.name == "files" and tool.name == "read_file":
        raw = str(args.get("path", ""))
        p = PurePosixPath(raw)
        if p.is_absolute() or ".." in p.parts or len(p.parts) != 2:
            raise ToolDenied("path must be '<container>/<file>' without traversal")
        if p.parts[0] not in readable_slugs or str(p) in excluded_paths:
            raise ToolDenied("resource outside your permissions")


def authorize(
    perms: EffectivePermissions,
    server: McpServer,
    tool: Tool,
    args: dict[str, Any],
    *,
    readable_slugs: set[str],
    excluded_paths: set[str],
) -> None:
    if not server.enabled or not tool.enabled or server.status != ServerStatus.approved:
        raise ToolDenied("tool is disabled")
    if tool.id not in perms.executable_tools:
        raise ToolDenied("tool not permitted")
    validate_arguments(tool, args)
    check_resources(server, tool, args, readable_slugs, excluded_paths)


def authorize_registered(
    perms: EffectivePermissions, server: McpServer, tool: Tool, args: dict[str, Any], classification: Classification
) -> dict[str, Any]:
    """Registered servers: approval state, grant, clearance, model-facing schema, constraints, full schema."""
    from app.mcp.review import model_schema

    if not server.enabled or server.status != ServerStatus.approved or not tool.enabled or tool.needs_review:
        raise ToolDenied("tool is disabled")
    if tool.id not in perms.executable_tools:
        raise ToolDenied("tool not permitted")
    check_clearance(server, classification)
    if any(k.startswith("_") for k in args):
        raise ToolDenied("reserved argument names are not allowed")
    for schema in (model_schema(tool),):
        try:
            errors = sorted(Draft202012Validator(strict_schema(schema)).iter_errors(args), key=str)
        except SchemaError as exc:
            raise ToolDenied("tool schema is invalid") from exc
        if errors:
            raise ToolDenied(f"invalid arguments: {errors[0].message}")
    final = apply_constraints(tool, args)
    validate_arguments(tool, final)
    return final

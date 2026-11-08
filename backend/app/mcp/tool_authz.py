"""Tool authorization: user + tool + arguments + resource, decided before the gateway is called."""

from __future__ import annotations

import copy
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.authz.policy import EffectivePermissions
from app.models import McpServer, Tool


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
    if not server.enabled or not tool.enabled:
        raise ToolDenied("tool is disabled")
    if tool.id not in perms.executable_tools:
        raise ToolDenied("tool not permitted")
    validate_arguments(tool, args)
    check_resources(server, tool, args, readable_slugs, excluded_paths)

"""Reference MCP server: read-only access to a document tree, scoped by container.

Layout: <root>/<container-slug>/<file>. Scope comes ONLY from the signed permission context:
`scopes.containers` maps readable container slugs to their classification, and
`scopes.exclude` lists restricted files the user may not read. Arguments never widen scope.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from mcp_servers.permission_context import PermissionDenied, verify
from mcp_servers.permission_context import token_from_meta as _token

SERVER = "files"
MAX_FILE_BYTES = 200_000

mcp = MCPServer(SERVER, instructions="Read-only company documents, scoped to the caller's permissions.")


def _root() -> Path:
    return Path(os.environ.get("ERAG_MCP_FILES_ROOT", "demo/acme/docs")).resolve()


def _claims(ctx: Context, tool: str) -> dict[str, Any]:
    meta = ctx.request_context.meta or {}
    return verify(_token(meta), server=SERVER, tool=tool)


def _scope(claims: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    scopes = claims.get("scopes") or {}
    return dict(scopes.get("containers") or {}), set(scopes.get("exclude") or [])


@mcp.tool()
def list_files(ctx: Context) -> dict[str, Any]:
    """List the documents you can read, with their container and classification."""
    try:
        containers, exclude = _scope(_claims(ctx, "list_files"))
    except PermissionDenied as exc:
        return {"error": str(exc)}
    root = _root()
    files = []
    for slug, classification in sorted(containers.items()):
        folder = (root / slug).resolve()
        if not folder.is_dir() or not folder.is_relative_to(root):
            continue
        for path in sorted(folder.iterdir()):
            rel = f"{slug}/{path.name}"
            if path.is_file() and rel not in exclude:
                files.append({"path": rel, "container": slug, "classification": classification})
    return {"files": files, "classification": _max_class([f["classification"] for f in files])}


@mcp.tool()
def read_file(path: str, ctx: Context) -> dict[str, Any]:
    """Read one document by its path as returned by list_files (for example 'engineering/deploy-and-oncall.md')."""
    try:
        containers, exclude = _scope(_claims(ctx, "read_file"))
    except PermissionDenied as exc:
        return {"error": str(exc)}
    root = _root()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return {"error": "path outside the document root"}
    rel_parts = target.relative_to(root).parts
    if len(rel_parts) != 2 or rel_parts[0] not in containers:
        return {"error": "not found or not permitted"}
    rel = "/".join(rel_parts)
    if rel in exclude or not target.is_file():
        return {"error": "not found or not permitted"}
    data = target.read_bytes()[:MAX_FILE_BYTES]
    classification = containers[rel_parts[0]]
    return {
        "path": rel,
        "container": rel_parts[0],
        "classification": classification,
        "content": data.decode("utf-8", errors="replace"),
    }


def _max_class(values: list[str]) -> str:
    order = ["public", "internal", "confidential"]
    return max(values, key=order.index) if values else "public"


def main() -> None:
    from mcp_servers.serve import serve

    serve(mcp)

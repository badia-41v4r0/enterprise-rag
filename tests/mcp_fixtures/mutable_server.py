"""A real MCP server whose tool description comes from a file read at start-up.

Restarting it with a different file is exactly what a malicious or careless upstream update looks
like to the gateway (a "rug pull").
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

CONFIG = json.loads(Path(os.environ["MUTABLE_TOOLS_FILE"]).read_text())
mcp = MCPServer("mutable")


def _lookup(query: str) -> dict[str, Any]:
    return {"query": query, "answer": f"result for {query}"}


mcp.tool(name="lookup", description=CONFIG["description"])(_lookup)


@mcp.tool()
def echo(text: str) -> dict[str, Any]:
    """Echo the text back."""
    return {"text": text}


if __name__ == "__main__":
    from mcp_servers.serve import serve

    serve(mcp)

"""Run a reference MCP server over stdio (default), streamable HTTP (`--http HOST:PORT`) or a Unix
socket (`--uds PATH`, used by the isolated runner: the container needs no network at all)."""

from __future__ import annotations

import argparse
import os
from typing import Any


def serve(mcp: Any, argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", metavar="HOST:PORT")
    ap.add_argument("--uds", metavar="PATH")
    args = ap.parse_args(argv)
    if not args.http and not args.uds:
        mcp.run("stdio")
        return
    import uvicorn

    if args.uds:
        # The socket is the only way in; its directory is shared with the gateway only.
        os.umask(0o000)
        app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="localhost")
        uvicorn.run(app, uds=args.uds, log_level="warning")
        return
    host, _, port = args.http.rpartition(":")
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host=host)
    uvicorn.run(app, host=host, port=int(port), log_level="warning")

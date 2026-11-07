# 0010. MCP over stdio with a signed permission context in `_meta`

**Status:** Accepted (2026-11-08)

## Decision
- The gateway is an MCP client (official `mcp` SDK 2.x). Reference servers run as stdio subprocesses spawned per call (`python -m mcp_servers.<name>`), which keeps Compose to one app container and gives each call a fresh process.
- The gateway attaches a short-lived permission context to every call in the request `_meta` under `erag/permission_context`: an HS256 JWT with `sub` (user), `aud` (server name), `tool`, `scopes` (allowed containers / departments), `iat`, `exp` (default 60 s) and `jti`. The key is derived per server: `HMAC(ERAG_MCP_CONTEXT_SECRET, server_name)`, so a token for one server is useless at another.
- Servers verify signature, audience, tool and expiry, and scope their data to the token *before* querying. The model never sees or produces the token; tool arguments that try to widen scope are ignored by design (scope comes only from the token).
- The result validator is the second barrier: schema, size cap, injection heuristics, classification tagging.

## Consequences
About 1 s of process start-up per tool call. Long-lived sessions or Streamable HTTP are a later optimization.

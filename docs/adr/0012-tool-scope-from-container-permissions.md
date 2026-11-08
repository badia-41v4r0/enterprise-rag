# 0012. Tool data scope is derived from container permissions

**Status:** Accepted (2026-11-08)

## Context
MCP servers must only receive data the user may already see. Tools need a scope (which files, which department records) that the server can enforce without trusting the model.

## Decision
- The scope inside the permission context is computed from the **same** `effective_permissions` used for retrieval:
  - `files`: `containers` = readable container slugs (with classification), `exclude` = restricted documents in those containers that the user may not read (`<slug>/<filename>`).
  - `tickets`: `departments` = departments whose mapped container the user can read (`hr -> hr-confidential`, `finance -> finance`, `engineering -> engineering`).
- `execute` grants decide whether a tool is visible and callable; the scope decides what data it returns. Revoking a container therefore also narrows every tool immediately.
- Tool arguments can never widen scope: the gateway enforces `additionalProperties: false`, rejects reserved `_`-prefixed names, checks tool-specific resources (e.g. `read_file` paths), and the servers read scope only from the signed token.
- Model-facing tool names are `<server>__<tool>` (OpenAI function-name charset). A hidden or non-existent tool called by name gets the same "unknown tool" refusal, so the model cannot probe what exists.

## Consequences
One source of truth for authorization. The department mapping lives in `app/mcp/registry.py` (code, not DB) for the MVP.

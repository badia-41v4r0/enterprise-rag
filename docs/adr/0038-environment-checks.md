# 0038. Environment checks: read-only, booleans and the exact fix, never values

**Status:** Accepted (2026-11-09), implements PLAN-v0.4 step 1

## Decision
`app/services/envcheck.py` runs, for step 1, the review step and the Setup assistant:

| Check | ok | not ok |
|---|---|---|
| Database | `alembic_version` equals the code's head | fail: unreachable or behind (fix: restart the app, it migrates) |
| Master key | loads, file mode 0600 | fail: cannot be loaded; warn: dev-derived key, or a file readable by others |
| Production mode | `ERAG_ENV=prod` | warn |
| Secrets | session, MCP context and (when the runner is configured) runner secrets are not `dev-insecure*` and have 32+ characters | fail, listing the variable names |
| HTTPS | secure cookies and an https request | warn on localhost; **fail** when reached from another address over plain HTTP |
| Storage | a file can be created in `ERAG_BLOB_DIR` | fail |
| Worker | heartbeat in `app_settings` younger than 2 minutes (the worker writes it every `ERAG_WORKER_HEARTBEAT_SECONDS`, 20) | fail |
| Ollama | `/api/tags` answers, through the same destination guard as Ollama providers | fail |
| Embedding model | `ERAG_EMBED_MODEL` installed | fail |
| Chat model | the default Ollama model (or `ERAG_LOCAL_CHAT_MODEL`) installed | fail |
| Runner | optional: not configured is `info`; configured but unreachable is `warn` | |

Each item is `{id, label, status, detail, fix}`; the detail names variables, paths and model names but never a
secret value. The wizard never edits environment-level settings. Failures do not block the wizard (it may be
completed and fixed later), except that a production setup cannot be finished while an administrator has a known
password (ADR 0037).

## Consequences
- Tests against real failures (`tests/setup/test_envcheck.py`): a closed port as the Ollama URL, model names that
  do not exist in the real Ollama, default and short secrets, a missing key file and a world-readable one, a
  read-only directory as storage, no heartbeat then a fresh one, a closed port as runner URL, and the endpoint's
  output checked for the absence of secret values.

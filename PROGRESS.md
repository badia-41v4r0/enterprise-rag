# EnterpriseRAG MVP: progress report

Branch: `feat/mvp` (not pushed). Plan: [`docs/PLAN.md`](docs/PLAN.md). Decisions: [`docs/adr/`](docs/adr/README.md).

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0. Foundations | Done | Compose, Alembic, config, CI workflow, ADRs |
| 1. Identity and permissions | Done | Sessions (argon2, HttpOnly cookie, CSRF), roles, containers, grants, `can()`, admin API, audit, CLI admin |
| 2. Ingestion | Done | Upload API, sha256 dedupe, versioning, LocalBlobStore, SKIP LOCKED worker, parsers (PDF/DOCX/XLSX/HTML/MD/TXT), chunker, bge-m3 embeddings |
| 3. Authorized retrieval and chat | Done | Filtered pgvector search, ACL re-check, context builder, prompt builder, Ollama/OpenRouter/claude-code providers, classification router, SSE chat, conversations; **leakage suite green** |
| 4. Frontend | Done | React SPA: login, streaming chat with clickable citations, model picker, tool/classification display, history; admin (users, roles, containers + uploads, grants incl. document/tool, tools, audit, jobs). Built after Phase 5. |
| 5. MCP | Done | Registry, gateway (MCP client, stdio), signed permission context, tool authz, tool loop with re-routing, result validator, per-tool audit; `files` and `tickets` reference servers |
| 6. ACME demo and hardening | Not started | |
| 7. Docs, release | Not started | |

## Verified (Phase 0)

- `uv run alembic upgrade head` / `downgrade base` / `upgrade head` round-trip on PostgreSQL 16 + pgvector.
- `APP_PORT=8088 docker compose up -d --build app` then `curl localhost:8088/health` returned `{"status":"ok","db":true}`.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest` pass locally.
- `uv run python scripts/check_licenses.py`: no copyleft dependencies.
- The GitHub Actions workflow (`.github/workflows/ci.yml`) was written but **not run** (nothing is pushed).

## Verified (Phase 1)

- `uv run pytest`: permission matrix (`tests/unit/test_policy.py`) and identity/admin API tests against real Postgres (`tests/integration/test_identity.py`): login/logout, CSRF rejection, login rate limit, admin-only routes, grant create/validate/duplicate/revoke with immediate effect, deactivation kills sessions, audit entries written.
- `uv run erag create-admin --email admin@acme.test --password ...` created the admin in the dev DB; second run reports "already exists".

## Verified (Phase 2)

- `uv run pytest`: parser tests for all six formats (PDF built by hand in the test, DOCX/XLSX generated), chunker, upload -> worker -> chunks with **real bge-m3**, identical file skipped, new version replaces only that document's vectors and marks the old version `superseded`, read-only users cannot upload, unsupported types rejected (415), path components stripped from filenames, two concurrent workers claim distinct jobs (`FOR UPDATE SKIP LOCKED`), unparseable file marks the version `failed`.
- Docker: `APP_PORT=8088 docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build app worker`; via curl: admin login, create container, upload a Markdown file -> `queued`; worker log shows the embedding call and `indexed version ... chunks: 1`; second upload of the same file -> `duplicate`; document list shows `latest_status: ready, chunk_count: 1`.

## Verified (Phase 3)

- `uv run pytest` (103 passed, 2 skipped: OpenRouter has no key; claude-code is opt-in). Security suite in `tests/security/`:
  - `test_vector_filter.py` (no model): an unauthorized chunk is never returned even when the query vector is *identical* to it; restricted documents need a document grant; empty permissions return nothing; grant removal is immediate; only current versions are searchable; the ACL re-check drops hits whose document changed after the search.
  - `test_canary_matrix.py` (real bge-m3): 6 personas x 8 questions (including "list every canary" and an override attempt): no canary of an unreadable container or restricted document appears in the built prompt; every context chunk belongs to a readable container; confidential context always routes local. Positive controls prove HR/Executive/Finance/Engineer do get their own canaries, and that Executive (container read only) does *not* get the restricted HR case. Citation endpoint returns 404 for unauthorized users (also for the admin, ADR 0009).
  - `test_revocation_and_history.py`: admin removes the HR role through the API; the next question loses HR context, and the earlier confidential answer in the same conversation is withheld from the prompt.
  - `test_routing.py`: router policy; a cloud provider can never be cleared for confidential; with only a cloud model, the HR salary question is refused with zero egress; with local+cloud and the user requesting the cloud model, the request goes to Ollama only; internal engineering context may go to the cloud and carries no HR/Finance canary (egress recorded and blocked by a test guard, ADR 0008).
  - `tests/unit/test_prompt.py`: untrusted text cannot close `<document>`/`<context>` delimiters; dedupe/budget/citations; `</think>` stripping.
- Real provider tests (`tests/providers/`, real `qwen3:4b`): streaming text without reasoning, a real `list_invoices` tool call, and the signature scenario end to end through `/api/chat` SSE.
- Docker (hostnet override, port 8088), curl against `/api/chat`: HR manager -> citations from *HR Confidential*, routed to `ollama:qwen3:4b` as `confidential`, answer "58,000 to 72,000 EUR [1]" (~11 s); Engineer -> citations only from *Engineering* (career ladder), answer "I don't have information about that in the sources available to you."

## Verified (Phase 5)

- `uv run pytest` (138 passed, 2 skipped). `tests/security/test_tools.py` runs against the **real** reference servers over stdio (no model):
  - visible tools per persona (engineer/new hire never see `tickets__list_invoices`); a hidden tool called by name gets the same "unknown tool" refusal as a non-existent one, audited as `deny`.
  - same call `tickets__search_tickets` returns ENG-only rows for the engineer, HR-only for HR, FIN-only for finance, all three for the executive, with the matching classification.
  - forged arguments (extra `department`, reserved `_permission_context`, wrong types, non-JSON, non-object) refused before the gateway.
  - `files__read_file`: out-of-scope container, `..` traversal, absolute path, and the restricted HR case for the executive are refused; HR can read it.
  - server-side barrier with tool_authz bypassed: a token scoped to engineering cannot read HR files or invoices; tampered, expired, wrong-audience, wrong-tool, wrong-secret and missing tokens are all rejected by the server.
  - injection in a ticket title is dropped and flagged; oversized results refused; per-user tool rate limit; re-routing to a local model after a confidential tool result (pure function).
- Real Ollama (`tests/providers/test_providers.py::test_tool_scenario_end_to_end`): the finance analyst's "open invoices over 10k" runs the real tool loop (`tickets__list_invoices`, result `confidential`, answer from the local model); the engineer is never offered the tool.
- Docker (hostnet, port 8088): `/api/tools` for finance lists 4 tools, for the engineer 3; finance chat -> `tool_call tickets__list_invoices {"min_amount":10000,"status":"open"}` -> `tool_result ok confidential` -> answer listing INV-2026-0914/0912/0911; engineer -> "I don't have information about that ...".

## Verified (Phase 4)

- `cd frontend && npm run build` (tsc strict + vite) passes.
- Docker image rebuilt with the multi-stage Dockerfile; FastAPI serves the SPA at `/` and `/api/*` 404s stay JSON.
- `scripts/ui_smoke.mjs` (playwright-core + system Chromium, against the Docker app on port 8088): admin logs in, creates a role, a container, uploads a Markdown file (worker ingests it: status `ready`), creates a user with that role, grants `read` on the container; the new user logs in, asks "Where do employees park?", gets "Employees park on level -2 [1]" from `ollama:qwen3:4b` tagged `internal`, clicks the citation and sees the source chunk. Output: `UI SMOKE PASSED`.
- Found and fixed with it: a `useEffect` returning the Promise of `scrollIntoView()` crashed React on unmount in current Chromium.
- Not exercised by an automated UI test: document/tool grants, tool enable/classification edits, audit/jobs views (they use the same API endpoints covered by backend tests).

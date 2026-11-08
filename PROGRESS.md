# EnterpriseRAG: progress report

## Post-MVP releases (PLAN-v0.2), branch `feat/admin-extensibility`

Plan: [`docs/PLAN-v0.2.md`](docs/PLAN-v0.2.md). Work done autonomously; every release section below lists what was verified, how, what was skipped, and open decisions. Nothing is pushed or tagged.

| Release | Status | Commit |
|---|---|---|
| R1 Roles, delegated management, uploads | Done | see `git log` ("R1: ...") |
| R2 Credential store and MFA | Done | "R2: ..." |
| R3 Connectors | Not started | |
| R4 MCP registry and clearance | Not started | |
| R5 Isolated runner | Not started | |

### R1. Roles, delegated management and uploads

**What was built**
- Grant action `contribute` (migration 0004) between `read` and `manage` (ADR 0017). `contribute`: upload, replace documents you can read, retry failed ingestion. `manage`: also delete, upload restricted documents, toggle `restricted` (lifting it only on documents you can read). Grants, classification, roles and users stay admin-only.
- Roles API: rename (`PATCH /api/admin/roles/{id}`), clone with grants (`POST .../clone`), templates Reader/Contributor/Manager on create or `POST .../apply-template`, bulk membership (`POST .../members`), matrix cell (`PUT /api/admin/roles/{id}/containers/{cid}` with `none|read|contribute|manage`), delete refused while users hold the role unless `?reassign_to=<role>`. Role list shows user and grant counts.
- Effective-permissions preview: `GET /api/admin/users/{id}/effective-permissions` (same function as the request path).
- Documents API: `POST /api/documents/{id}/versions` (replace, keeps the stored filename), `POST /api/documents/{id}/retry` (failed latest version), version history with uploader and current flag, latest error, `can_read` per document; dedupe never matches restricted documents the uploader cannot read (no read oracle).
- UI: new **Content** page (shown to admins and to anyone with `contribute`/`manage`): multi-file drag and drop, per-file upload status, live document status (queued/processing/ready/failed with error), version history, replace, retry, delete, restricted toggle. Admin: roles screen (template, rename, clone, members, delete with reassign), **grants matrix** (role x container level, role x tool execute) and effective-permissions preview. `/api/auth/me` returns `can_manage_content`.
- Demo: new persona `hr.assistant@acme.test` (role *HR Contributor*: `contribute` on HR Confidential, no access to the restricted HR case). The canary matrix now runs for 7 personas.

**Verified (2026-11-09)**
- `uv run pytest`: **170 passed, 2 skipped** (OpenRouter and claude-code opt-in) in 113 s. New `tests/security/test_delegation.py` (7 tests): a manager manages only their container (upload, replace, restrict, delete; other container 404); a manager cannot create grants, change classification, set matrix levels, list users, read a restricted document's chunk, or lift `restricted` on a document they cannot read (also not by re-uploading it with `restricted=false`); a contributor can upload and replace but not delete, restrict, upload restricted, replace or see a restricted document; dedupe is not an oracle; removing the `manage` grant through the admin API takes effect on the next request; role create-from-template, rename, clone, matrix level replace, bulk members, preview, delete refused in use then allowed with reassign; failed ingestion retry; all actions audited.
- `uv run ruff check .`, `ruff format --check .`, `mypy` (strict), `scripts/check_licenses.py`, `npm run build`: pass.
- Docker: `ERAG_LOCAL_CHAT_MODEL=qwen3:4b-instruct-2507-q4_K_M APP_PORT=8088 docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build`; log: `Running upgrade 0003 -> 0004`, `ACME seeded: 5 containers, 7 personas`.
- `scripts/ui_smoke.mjs` (extended) against the stack: admin creates role, container, uploads (new drop zone input), user, read grant **through the matrix**, preview shows it; the new user gets "Employees park on level -2 [1]"; **R1 exit:** `hr@acme.test` (manage on HR Confidential) opens Content, sees only HR Confidential, uploads a document, replaces it (2 versions, ready), deletes it, and has no Admin tab. `UI SMOKE PASSED`.
- `scripts/demo_scenarios.py --base http://127.0.0.1:8088`: all three scenarios unchanged (HR gets 58,000 to 72,000 EUR from the local model, engineer and new hire get "no information"; finance invoices tool call; revocation).

**Decisions:** ADR 0017 (contribute level, un-restrict rule, metadata vs content for managers, dedupe oracle, replace-by-name rule, templates as presets, delete with reassign).

**Known issues / notes**
- A manager without a document grant who restricts a document cannot un-restrict it again (by design, ADR 0017); the admin can.
- Uploading a file named like a restricted document you cannot read returns 403, which reveals the name exists.
- `deploy/.env` on this machine sets `ERAG_LOCAL_CHAT_MODEL=nvidia/nemotron-3.5-lightning:free` (an OpenRouter id), so the stack's local model returned HTTP 404 until I started compose with `ERAG_LOCAL_CHAT_MODEL=qwen3:4b-instruct-2507-q4_K_M` in the shell (shell variables override the file). I did not edit `deploy/.env`.

### R2. Credential store and MFA

**What was built**
- Credential store (ADR 0018): table `secrets` (migration 0005), AES-256-GCM with the name as associated data, master key from `ERAG_SECRETS_KEY` / `ERAG_SECRETS_KEY_FILE` (required in prod; Compose uses `/data/secrets.key`, generated on first start in dev), key id per ciphertext, write-only admin API (`PUT/DELETE /api/admin/secrets`, list with "used by"), `secret.use` audit with the consumer, `reveal_assigned` for consumers with assigned secrets, `erag secrets init|set|list|rotate-master`.
- OpenRouter: key looked up in the vault first, env var as bootstrap fallback; Providers screen (`GET/PUT /api/admin/providers/openrouter`) sets key, model, on/off and ceiling; router cached and rebuilt when the setting or the secret changes (no restart). Free-only and the env ceiling stay hard limits (UI can only lower the ceiling).
- MFA (ADR 0019): optional TOTP for everyone, QR (SVG) + manual key, 10 recovery codes (hashed, single use), two-step login with a pending session that grants nothing, attempt limit and rate limit, replay protection by time step, session rotation after the second factor, admin reset (step-up) and `erag reset-mfa`.
- Step-up: `POST /api/auth/step-up` (password, or TOTP when MFA is on); required within 5 minutes for secrets, provider settings, creating/promoting admins and MFA resets (`403 step_up_required`, the UI prompts and retries).
- UI: Account page (MFA setup/disable/new recovery codes), MFA step on the login page, step-up dialog, admin Secrets and Providers tabs, MFA column + reset on Users.
- Fixed on the way: logout did not return to the login page (the `me` query observer was detached by `queryClient.clear()`; an MVP bug the old smoke test did not exercise).

**Verified (2026-11-09)**
- `uv run pytest`: **178 passed, 2 skipped**. New `tests/security/test_vault_and_mfa.py` (8 tests): cipher binds name and key and detects tampering; secrets API write-only, audited, ciphertext swap detected, a scan of every table finds neither the plaintext nor its hex; step-up required for 6 sensitive endpoints after the 5-minute window, wrong password refused, then allowed; OpenRouter vault-first/env-fallback, paid model and ceiling above the cap refused, hot reload of `/api/models`, disable; MFA enrolment with codes computed from the secret decrypted from the DB, password-only session gets 401 everywhere, replayed code refused, next step accepted, session cookie rotated, recovery code works once; 6th wrong code gives 429 and kills the pending session; step-up with MFA needs TOTP (password refused); admin reset ends sessions; `python -m app.cli reset-mfa` works; master key rotation.
- `ruff`, `ruff format --check`, `mypy` strict, license check (new deps: `cryptography` Apache-2.0/BSD, `pyotp` MIT, `segno` BSD), `npm run build`: pass.
- **Exit criterion, OpenRouter key only in the vault:** stack rebuilt (`Running upgrade 0004 -> 0005`, `Generated a new credential-store master key at /data/secrets.key`); `docker exec enterprise-rag-app-1 python -m app.cli secrets set openrouter_api_key --from-env ERAG_OPENROUTER_API_KEY` (value never printed); restarted with `ERAG_OPENROUTER_API_KEY=` (verified empty inside the container); `GET /api/admin/providers/openrouter` -> `configured: true, key_source: vault`; model set to `nvidia/nemotron-3.5-lightning:free` through the API; **one real request** as the new hire (public context) routed to `openrouter:nvidia/nemotron-3.5-lightning:free` and answered from the release notes; audit shows `secret.use` by `provider:openrouter`; recorded spend 0.0 USD.
- **Exit criterion, login with and without MFA:** `scripts/ui_smoke.mjs` now also stores and deletes a secret (value never in the page), reads the provider status ("configured (credential store)"), and the new user enables MFA (QR + recovery codes), logs out, and logs in with password + a TOTP computed in Node. Admin and HR log in without MFA. `UI SMOKE PASSED`.

**Decisions:** ADR 0018 (dev key derivation and dev key-file bootstrap, key id, router fingerprint cache, UI ceiling can only lower the env cap), ADR 0019 (fresh login counts as recent auth; password not accepted for step-up when MFA is on; recovery codes as HMAC; disabling MFA needs a code).

**Known issues / notes**
- Secret values are decrypted when the provider router is rebuilt, not per request; each rebuild is audited.
- The stack on this machine must currently be started with `ERAG_OPENROUTER_API_KEY=` (empty) to prove vault-only operation, because `deploy/.env` still holds the key. **Owner:** remove the key from `deploy/.env` now that it is in the vault (I did not edit that file).

---

# MVP progress report (v0.1.0)

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
| 6. ACME demo and hardening | Done | Seed + personas + synthetic docs with canaries, one-command seeded demo (`ERAG_SEED_DEMO`), scripted scenarios, rate limits, security headers, request logs, prod secret check. OpenRouter spend ceiling **not** implemented. |
| 7. Docs, release | Partial | README, architecture (Mermaid), threat model, white paper draft, SECURITY.md, CONTRIBUTING.md, issue templates, ROADMAP, screenshots. **Not done:** demo GIF/video, editable English Excalidraw diagram, `v0.1.0` tag (owner decision). |

## Run the demo

Requirements: Docker Compose v2; Ollama on the host with `bge-m3` and `qwen3:4b-instruct-2507-q4_K_M` pulled (both present on this machine; `qwen3:4b`, the thinking variant, is no longer used and can be removed).

```bash
cd ~/Desktop/enterprise-rag
git checkout feat/mvp
# Port 8000 is taken on this machine by another service, hence APP_PORT=8088.
APP_PORT=8088 docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build
# open http://127.0.0.1:8088  (hr@ / engineer@ / finance@ / newhire@ / exec@ / admin@acme.test, password acme-demo-password)
uv run python scripts/demo_scenarios.py --base http://127.0.0.1:8088     # the three scripted scenarios (~3 min)
```

Tests and checks:

```bash
uv sync && docker compose -f deploy/docker-compose.yml up -d postgres
uv run pytest            # ~4 min with Ollama up (real bge-m3 + qwen3:4b); Ollama tests skip if it is down
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run python scripts/check_licenses.py
cd frontend && npm ci && npm run build
# UI smoke (needs playwright-core somewhere on NODE_PATH and a Chromium):
npm install --prefix .scratch/e2e playwright-core
NODE_PATH=.scratch/e2e/node_modules CHROMIUM=/usr/bin/chromium BASE=http://127.0.0.1:8088 node scripts/ui_smoke.mjs
```

The stack is left **running** (containers `enterprise-rag-postgres-1`, `-app-1`, `-worker-1`). Stop it with
`docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml down` (add `-v` to delete the demo data).

## Decisions taken (ADRs)

All in [`docs/adr/`](docs/adr/README.md): query-time ACL (0002), routing after retrieval and after tool results (0003), asyncpg instead of LGPL psycopg (0004), storage interfaces (0005), host Ollama + hostnet override (0006), `qwen3:4b` local model (0007), testing without fake providers + egress guard (0008), admin is not data access (0009), MCP over stdio with signed `_meta` context (0010), own OpenAI-compatible client (0011), tool scope from container permissions (0012), validator heuristics (0013), frontend (0014), seeded demo + hardening (0015).

Other notable choices: Phase 5 (MCP) was done before Phase 4 (frontend) because its security tests are core; ACME documents and the seed script were pulled forward into Phase 3 because the canary matrix needs them; retrieval similarity floor 0.45 calibrated on the ACME corpus; with no authorized context and no tools, no model is called (fixed "no information" answer).

## Skipped / not done (and why)

- Hybrid search and reranker: first two items of the PLAN cut list; not needed for the demo.
- OpenRouter: provider implemented, but **never called for real** (no API key; budget rule). Its tests skip. The monthly spend ceiling setting exists but is **not enforced**.
- `claude-code` dev provider: implemented, disabled by default; its opt-in test was **not run** (to save subscription quota).
- Bundled Ollama container (`--profile ollama`): written, **not run** (would duplicate several GB of models).
- GitHub Actions CI: workflow written, **never run** (nothing pushed). The same commands pass locally.
- Demo GIF/video, English Excalidraw diagram, `v0.1.0` tag: left for the owner.

## Known issues

- `qwen3:4b` is the 2507 *thinking* variant: it always reasons first, so answers take ~10 s and tool answers 40 to 90 s on the GTX 1660 SUPER. Reasoning is stripped before display/storage. See ADR 0007.
- Small-model answer quality varies (e.g. the engineer's salary answer sometimes says "not specified ... handled by HR" instead of the exact "no information" sentence). Tests never assert on wording, only on what the model was given.
- When a user has tools but no relevant documents, the model is still called (it may decide to use a tool), so the "no model called" shortcut only applies to users without tools.
- Injection filter drops whole strings and can have false positives (ADR 0013).
- Seeding from the host into a DB used by the containers fails ingestion (blobs on different filesystems); seed inside the containers (default bootstrap) or run the worker on the host too.
- Rate limiters are in-process (single instance).
- The UI smoke test leaves its test role/user/container in the dev database.

## Update 2026-11-08 (after owner review)

- Local model switched to the non-thinking `qwen3:4b-instruct-2507-q4_K_M` (ADR 0007). Full suite: **147 passed, 2 skipped in 95 s** (was 140 in 4 min 47 s); the real-Ollama provider tests (streaming, real tool call, signature scenario) pass with it.
- OpenRouter: free models only (`:free`, enforced at startup and per request) and a hard monthly spend ceiling of `0.01` USD stored in `provider_spend` (migration 0003, ADR 0016, `tests/integration/test_spend.py`). Still **never called for real**: it needs `ERAG_OPENROUTER_API_KEY`; the `provider_openrouter` test skips without it. The ceiling is covered by tests against real Postgres, but the cost field returned by OpenRouter (`usage.cost`) has not been seen in a real response yet.
- Owner decisions: commit attribution ignored; admin has no implicit data access but assigns roles, grants and creates users (confirmed, ADR 0009); tag/push at the end.
- Still open: decision 4 below (what the cloud may receive).

## Decisions for the owner (original list; 1, 2, 3, 6 resolved above)

1. **Commit attribution:** commits end with `Co-Authored-By: Claude Sonnet 5.5`, exactly as the task instructed, although the work was done by Claude Opus 5.5. Amend if you want accurate attribution.
2. Swap the local model to the non-thinking `qwen3:4b-instruct-2507` for 3-5x faster demos (one `ollama pull` + `ERAG_LOCAL_CHAT_MODEL`). ADR 0007.
3. Admin has no implicit data access (ADR 0009). Keep, or give admins read-all?
4. Cloud may receive `internal` data by default (`ERAG_CLOUD_MAX_CLASSIFICATION=internal`). Keep or tighten to `public`?
5. The compose file seeds the demo by default (ADR 0015). Fine for a reference repo; turn off for real deployments.
6. Add an OpenRouter key (prepaid, capped) to run `provider_openrouter` tests and the cloud half of the routing demo; implement the spend ceiling first.
7. Enable GitHub private vulnerability reporting (SECURITY.md points to it) and review the white paper draft (`docs/whitepaper/whitepaper.md`).
8. Tag `v0.1.0` and push when satisfied (nothing was pushed).

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

## Verified (Phase 6)

- `uv run pytest`: **140 passed, 2 skipped** (OpenRouter: no key; claude-code: opt-in) in 4 min 47 s with real Postgres, bge-m3, qwen3:4b and MCP servers. Includes security headers and the prod secret check.
- Fresh one-command bring-up: `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml down -v`, then `up -d --build` with `APP_PORT=8088`: `/health` OK, app log `ACME seeded: 5 containers, 6 personas, 12 documents queued ... 8 tool grants`, all 12 ingestion jobs `done` **21 s** after `up` (Docker image cached; models already pulled). A cold first build adds the image build (a few minutes) plus model downloads (3.7 GB), so "under 10 minutes" depends on bandwidth and is not verified on a clean machine.
- `uv run python scripts/demo_scenarios.py --base http://127.0.0.1:8088` (2 min 50 s), real output:
  - salary question: HR -> "58,000 to 72,000 EUR [1]" from *HR Confidential*, `ollama:qwen3:4b`, `confidential`; engineer -> only *Engineering* career ladder, "not specified ... handled by HR"; new hire -> "I don't have information about that in the sources available to you."
  - invoices: finance sees 4 tools, model calls `tickets__list_invoices {"min_amount":10000,"status":"open"}` and lists INV-2026-0914/0912/0911; engineer sees 3 tools (no invoices) and gets "I don't have information ...".
  - revocation: after the admin removes the HR role, HR's salary question returns no sources and "I don't have information ..."; role restored.
- UI smoke re-run with the CSP active: `UI SMOKE PASSED`.

## Verified (Phase 7)

- Docs written: README (quick start, development; licensing section unchanged byte for byte), `docs/architecture.md` (Mermaid, not rendered/checked visually), `docs/threat-model.md`, `docs/whitepaper/whitepaper.md` (draft), `SECURITY.md`, `CONTRIBUTING.md` (no external PRs until a CLA), issue templates, `ROADMAP.md`, `demo/acme/README.md`, screenshots in `docs/media/`.

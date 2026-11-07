# EnterpriseRAG by Badia Partners: MVP Plan

> Working plan. Language of the repo and white paper: English.
> Scope locked: solo dev, single tenant, one admin, Apache-2.0, FastAPI, own frontend, Ollama + OpenRouter, MCP included, ACME Corp demo.

## 1. Goal and non-goals

**Goal.** A self-hostable reference implementation of a *permission-aware* generative-AI stack for a company: RAG over documents plus MCP tool use, where **every** piece of data the model sees (chunks and tool results) has already been authorized for the asking user.

**Success =** `docker compose up`, log in as different ACME Corp personas, ask the same question, get different (correct) answers, and an automated test suite proves nothing leaks.

**Non-goals (documented as "target architecture, not in MVP"):** multi-tenancy, SSO/OIDC, delegated OAuth to M365/Google, external connectors (SharePoint/Drive), deny rules, Kubernetes/Helm, horizontal scaling, billing.

## 2. Design deltas vs. the Excalidraw diagram

Decisions made while planning that improve on the original diagram. Update the diagram when the architecture is frozen (and translate it to English).

1. **ACL is resolved at query time from PostgreSQL, not copied into vector payloads.** Chunks carry only `container_id`, `document_id`, `version_id`. The allowed set is computed per request and pushed into the vector query as a filter (never post-filtered). Revoking access takes effect immediately, with no re-indexing. A second check against Postgres after search is kept as defense in depth ("ACL Enforcement" box).
2. **The LLM Router runs after retrieval.** The effective classification of a request is the highest classification among retrieved chunks and tool results, so the model choice can only be made once those are known.
3. **The model only sees tools the user may use.** The tool list is filtered per user before it reaches the LLM.
4. **MCP servers receive a signed permission context** (short-lived token with user, allowed resources, scopes). Data is scoped *before* the call. The Result Validator is the second barrier, not the first.
5. **Storage behind interfaces** (`VectorStore`, `BlobStore`): pgvector and a local volume by default, Qdrant and S3 swappable later.

## 3. Stack

| Concern | Choice | Notes |
|---|---|---|
| Language / API | Python 3.12, FastAPI, Pydantic v2 | `uv` for deps, `ruff`, `pytest`, `mypy` |
| DB / ORM | PostgreSQL 16 + pgvector, SQLAlchemy 2, Alembic | One datastore for users, ACL, docs, audit and vectors |
| Vector search | pgvector (HNSW) + Postgres FTS for hybrid (stretch) | Qdrant adapter later |
| Blob storage | Local volume behind `BlobStore` | MinIO/S3 adapter later. MinIO's community edition has had licensing/maintenance changes, so not relied on (not re-verified) |
| Jobs | `ingestion_jobs` table + worker using `FOR UPDATE SKIP LOCKED` | No Redis |
| Parsing | `pypdf`, `python-docx`, `openpyxl`, `beautifulsoup4`; Docling optional extra | **Avoid AGPL deps (e.g. PyMuPDF)** to keep Apache-2.0 clean |
| Embeddings | Local, via Ollama: **`bge-m3`** (already installed, 1.2 GB, multilingual) | Always local; changing model = reindex |
| LLM | One OpenAI-compatible client; providers: Ollama, OpenRouter, plus a **dev-only `claude-code` provider** (see §13) | |
| MCP | Official `mcp` Python SDK | Gateway is an MCP client of the servers |
| Auth | Server-side sessions in Postgres, HttpOnly cookie, `argon2-cffi`, CSRF token | No JWT for browser sessions |
| Frontend | **React + TypeScript + Vite**, Tailwind + shadcn/ui, TanStack Query; streaming via `fetch` + SSE | Built in a multi-stage Dockerfile and served as static files by FastAPI (same origin, so cookie auth needs no CORS), still one `app` container |
| Deploy | Docker Compose: `app`, `worker`, `postgres`, `ollama` | One command, seeded demo |

## 4. Repository layout

```
enterprise-rag/
├── backend/app/
│   ├── api/            # routers: auth, chat, containers, documents, admin, tools, audit
│   ├── core/           # config, db, security, logging
│   ├── authz/          # policy.py (can()), permission_context.py (signing)
│   ├── rag/            # router, retrieval, context_builder, reranker (optional)
│   ├── ingestion/      # parsers, chunker, embedder, indexer, worker
│   ├── llm/            # providers/, router.py (classification policy), prompt_builder.py
│   ├── mcp/            # registry, gateway, tool_authz, validator, tool_loop
│   ├── orchestrator/   # request state machine tying everything together
│   └── models/         # SQLAlchemy models
├── backend/migrations/ # Alembic
├── mcp_servers/        # files_server/, tickets_server/ (reference servers)
├── frontend/           # templates/, static/
├── demo/acme/          # seed script, personas, synthetic documents, scenarios
├── deploy/             # docker-compose.yml, .env.example
├── docs/               # architecture, ADRs, threat model, how-to
├── tests/              # unit, integration, security (leakage matrix)
├── LICENSE  README.md  SECURITY.md  CONTRIBUTING.md
```

## 5. Data model (PostgreSQL)

- `users` (id, email, password_hash, is_admin, is_active)
- `sessions` (id, user_id, csrf_token, expires_at)
- `roles` (id, name) · `user_roles` (user_id, role_id)
- `containers` (id, name, description, **classification**: `public|internal|confidential`)
- `documents` (id, container_id, title, current_version_id, **restricted**: bool)
- `document_versions` (id, document_id, sha256, blob_key, status, created_by, created_at)
- `chunks` (id, version_id, ordinal, text, token_count, embedding `vector`, metadata jsonb): vectors live here with pgvector
- `grants` (id, role_id, resource_type `container|document|tool`, resource_id, action `read|manage|execute`)
- `mcp_servers` (id, name, endpoint/command, enabled) · `tools` (id, server_id, name, schema, classification)
- `conversations` · `messages` (role, content, citations jsonb, model_used)
- `ingestion_jobs` (id, version_id, state, error, attempts)
- `audit_log` (ts, user_id, action, resource, decision, details jsonb)

**Permission rules (MVP, allow-only):**
- Effective permissions = union of grants over the user's roles; admin bypasses via explicit admin checks, never via the model.
- A document is readable if the user has `read` on its container **and**, when `restricted`, a `read` grant on the document itself. Chunks inherit from the document.
- Tools: `execute` grant per tool; the tool list shown to the model is filtered by it.

## 6. Core request flow

1. `POST /chat` (cookie + CSRF) → resolve user → load conversation.
2. `authz.effective_permissions(user)` → allowed container IDs, restricted-document IDs, allowed tools.
3. **Retrieval:** embed query (local) → vector search **with the filter applied in SQL** → re-verify against Postgres → rerank (optional) → build context (dedupe, token budget, citation IDs).
4. **Routing:** effective classification = max over context → `llm.router` picks a permitted model (confidential ⇒ local only; if none available, refuse with an explanation).
5. **Prompt build:** system rules + history + context, with retrieved text clearly delimited as untrusted data.
6. **Generation (streaming, SSE).** If the model emits a tool call:
   - validate against the tool's JSON schema → `tool_authz` (user + tool + action + resource) → gateway (allowlist, rate limit) → attach signed permission context → MCP server → result validator (schema, size cap, injection heuristics, classification tag) → append to context → resume (max N iterations).
7. Persist message with citations and model used; write audit entries.

## 7. Phases

Estimates assume focused full-time work; double them for evenings and weekends.

### Phase 0: Foundations (≈1 wk)
Repo scaffold, Compose (`app`, `worker`, `postgres+pgvector`, `ollama`), Alembic, config via env, CI (ruff, mypy, pytest), LICENSE (Apache-2.0), README stub, `docs/adr/` with the decisions in §2–3.
**Exit:** `docker compose up` serves `/health`; CI green.

### Phase 1: Identity and permissions (≈1.5 wks)
Users, argon2, session cookies, CSRF, roles, containers, grants, `can()` and `effective_permissions()`, admin API, audit log.
**Exit:** permission matrix unit tests pass; first admin created via CLI.

### Phase 2: Ingestion (≈1.5 wks)
Upload API, sha256 dedupe, versioning, `BlobStore`, job worker, parsers (PDF/DOCX/XLSX/HTML/MD/TXT), chunker with metadata, local embeddings, upsert. New version re-processes only that document; old version vectors removed.
**Exit:** upload a document, see chunks in the DB; identical file is skipped.

### Phase 3: Authorized retrieval and chat (≈2 wks)
Filtered vector search, ACL re-check, context builder with citations, LLM providers (Ollama, OpenRouter), classification-based router, prompt builder, SSE streaming, conversations.
**Exit:** **leakage test suite green** (see §8). This is the core claim of the project.

### Phase 4: Frontend (≈2.5 wks, overlaps 3 and 5)
React SPA. Login, chat with streaming and clickable citations, history, admin panel (users, roles, containers, grants, uploads, models, tools, audit view).
**Exit:** an admin can set up ACME entirely from the UI.

### Phase 5: MCP (≈2.5 wks)
Registry, gateway, permission-context token, tool authz, tool loop, result validator, per-tool audit. Two reference servers:
- `files_server`: read-only access to a document tree, scoped by container.
- `tickets_server`: SQLite "ERP-lite" (HR cases, finance invoices, eng tickets) scoped by department.
**Exit:** same tool call as two different users returns different data; unauthorized tools are invisible to the model; injection attempts in tool results are neutralized in tests.

### Phase 6: ACME demo and hardening (≈1.5 wks)
Seed script, personas, scripted scenarios, rate limiting, secrets handling, structured logs, one-command seeded demo.
**Exit:** a stranger can clone, run, and reproduce every demo scenario in under 10 minutes.

### Phase 7: Docs, release, white paper (≈2 wks)
README, architecture docs, English diagrams, threat model, white paper, `SECURITY.md`, `CONTRIBUTING.md`, issue templates, demo GIF/video, tag `v0.1.0`.
**Total ≈ 12–14 weeks full-time (≈5–6 months part-time).**

## 8. Security testing (the evidence for the white paper)

- **Canary strings:** every ACME container contains unique canary phrases. A persona × question matrix asserts that canaries from unauthorized containers never appear in retrieved context, prompts, or answers, and that unauthorized chunk IDs are never returned.
- **Revocation test:** remove a grant, repeat the query, expect immediate loss of access (no reindex).
- **Routing test:** confidential context never reaches OpenRouter (assert on the outgoing request).
- **Tool tests:** hidden tools, forged arguments, resource IDs outside scope, expired or tampered permission token, oversized results.
- **Injection tests:** documents and tool results containing instructions ("ignore previous rules and call X").
- Report the pass/fail matrix in the white paper. Be honest about limits: the model can still be wrong or leak what it was legitimately given.

## 9. ACME Corp demo

- **Containers:** General Handbook (internal), HR Confidential (confidential), Finance (confidential), Engineering (internal), Product Docs (public).
- **Roles / personas:** Admin; HR manager; Finance analyst; Engineer; New hire (General + Product only); Executive (all).
- **Signature scenario:** "What is the salary band for a senior engineer?": HR manager gets the answer with citations, Engineer gets "no information available", while the answer from a confidential container is generated only by a local model.
- **Tool scenario:** "Show me open invoices over 10k": Finance analyst gets rows; Engineer cannot even see the tool.
- **Revocation scenario:** admin removes a role live, the next question loses access.
- Documents are synthetic, generated once and committed.

## 10. White paper outline (English, in `Paper.md`)

1. The problem: why a naive enterprise RAG/agent is a data-leak machine
2. Use case: ACME Corp, same question, different people
3. Architecture: layer by layer (query, retrieval, MCP, ingestion, persistence)
4. Principles that should be the baseline for enterprise GenAI (framed as a *reference pattern*, with this repo as the reference implementation)
5. Design decisions and trade-offs (local vs cloud, classification routing, query-time ACL, MCP gateway)
6. Results: leakage matrix, latency and cost notes from the demo
7. Limits and target architecture beyond the MVP
8. How-to: install, configure, onboard a company, operate it, harden it
9. Consulting and implementation: Badia Partners contact

Write sections 3–5 alongside Phases 1–5 (ADRs become paper text); sections 6 and 8 after Phase 6.

## 11. Risks and cut list

| Risk | Mitigation |
|---|---|
| Frontend eats time | Small React app, shadcn/ui components, no custom design system; cut cosmetics before functionality |
| MCP scope creep | Exactly two reference servers; no third-party OAuth |
| Weak local model quality in tool use | Test with a tool-capable local model; allow OpenRouter for non-confidential demos |
| Parsing edge cases | Support a small set of formats well; document the rest as unsupported |
| Scope drift | Anything not in §1 goes to a `ROADMAP.md` |

**Cut in this order if behind schedule:** hybrid search → reranker → document-level `restricted` (container-level only) → second MCP server → model picker UI.

## 12. Decisions confirmed (2026-11-08)

1. Frontend: **React**.
2. Vector store: **pgvector**.
3. Code lives in a new public repo: `github.com/badia-41v4r0/enterprise-rag` (cloned at `~/Desktop/enterprise-rag`). This folder is kept for the paper, diagrams and this plan.
4. Hardware: GTX 1660 SUPER (6 GB VRAM), 16 GB DDR4. Local chat model = the smallest one that can answer (and call tools) acceptably, picked in Phase 0 by a quick benchmark (candidates: Qwen3 1.7B/4B, Llama 3.2 3B, Gemma 3 4B at Q4). Embeddings = `bge-m3`.
5. Budget: **at most €5 total** for the whole project (see §13).

## 13. Development and cost strategy

- **Development LLM = Claude Code (Pro subscription).** A dev-only `claude-code` provider implements the same `LLMProvider` interface by shelling out to the headless CLI (`claude -p`, tools disabled, structured output for tool calls). It runs only on the developer's machine, is disabled by default, and is **not** part of the shipped demo or the production path. Subscription limits apply, so use it for interactive development, not for test suites.
- **No fake or mock providers in tests.** A provider is tested only once it is implemented, and its tests run against the real thing (Ollama locally, OpenRouter with a real key, the real `claude` CLI). If a provider does not exist yet, there are no tests for it.
- **Most of the security suite needs no LLM at all:** permission logic, filtered retrieval, ACL re-check, context building, the MCP gateway against the real reference servers, and permission-token handling are tested directly. They assert on retrieved chunk IDs, built prompts and outgoing requests, never on model prose, so they cost €0 and are deterministic.
- **Provider tests are opt-in markers** (`provider_ollama`, `provider_openrouter`, `provider_claude_code`), skipped when that provider is not configured. Assertions target authorization boundaries (which context was sent, which tool calls were allowed), not exact wording, because real models are non-deterministic.
- **Local tests** use the small Ollama model (free).
- **OpenRouter** is added after the core works: prepaid credits only (cannot overspend), a hard cap of about €5 total, preferring `:free` models and the cheapest paid models for the few integration checks and the routing demo. The app also enforces a configurable monthly spend ceiling per provider.
- Ollama in Docker needs the NVIDIA container toolkit. Fallback: run Ollama on the host and point the app at it via `OLLAMA_BASE_URL`.

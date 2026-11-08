# Architecture

English rendering of the original architecture diagram, updated with the design deltas of [PLAN.md section 2](PLAN.md) and the v0.2 extensions of [PLAN-v0.2.md](PLAN-v0.2.md). Decisions are in [`adr/`](adr/README.md).

```mermaid
flowchart LR
    U[Browser: React SPA] -- "HttpOnly session cookie + CSRF<br/>(+ TOTP step, step-up)" --> API

    subgraph App["app container (FastAPI)"]
        API[API routers<br/>auth, chat, content, admin,<br/>connectors, registry, secrets] --> ORCH[Chat orchestrator]
        ORCH --> AUTHZ["authz.effective_permissions()<br/>read / contribute / manage / execute<br/>(from Postgres, every request)"]
        ORCH --> RET[Retriever]
        RET -- "query vector + ACL filter in SQL" --> PG
        RET --> RECHECK[ACL re-check]
        RECHECK --> CTX[Context builder<br/>dedupe, budget, citations]
        CTX --> ROUTER[Classification router<br/>max(context, history, tool results)]
        ROUTER --> PROMPT[Prompt builder<br/>untrusted data delimited]
        PROMPT --> LLM[OpenAI-compatible client<br/>+ egress hook + destination guard<br/>+ spend ceiling + daily cap]
        REG[Provider registry<br/>locality from address,<br/>clearance cap, default model] -.-> ROUTER
        ORCH --> TOOLS[Tool runtime]
        TOOLS --> TAUTHZ[Tool authz<br/>grant, schema, constraints,<br/>clearance, approval state]
        TAUTHZ --> GW[MCP gateway<br/>stdio / HTTP / Unix socket,<br/>rate limits, rug-pull hash check,<br/>signed context for built-ins]
        GW --> VAL[Result validator<br/>size, label, injection]
        VAL --> ROUTER
        VAULT[Credential store<br/>AES-256-GCM, audited use] -.-> LLM
        VAULT -.-> GW
        NG[SSRF guard<br/>public-only, IP pinning]
    end

    KEY[(Master key file<br/>outside the DB)] -.-> VAULT
    LLM -- "any classification" --> OLLAMA[(Ollama, local<br/>qwen3:4b-instruct, bge-m3)]
    LLM -- "public / internal only<br/>key from the vault" --> OR[(OpenRouter, free models)]
    LLM -- "local: any classification<br/>(private address, Ollama or self-hosted)" --> LANLLM[(Admin-added local providers<br/>Ollama, vLLM, LM Studio)]
    LLM -- "cloud: up to the provider's clearance<br/>(public by default), HTTPS, pinned IP" --> APILLM[(Admin-added OpenAI-compatible APIs)]
    GW -- "stdio + _meta token" --> FILES[files_server]
    GW -- "stdio + _meta token" --> TICKETS[tickets_server]
    GW -- "HTTPS via SSRF guard<br/>clearance + label" --> REMOTE[Registered remote MCP]
    GW -- "HTTP over Unix socket" --> MCPC

    subgraph RunnerBox["runner service (only holder of the Docker socket)"]
        RUN[Runner API<br/>token auth, image allowlist<br/>pinned by ID, idle reaper]
    end
    API -- "ensure / stop / status / logs" --> RUN
    RUN -- "Docker Engine API" --> MCPC[MCP container<br/>network none, read-only root,<br/>no caps, non-root, limits]

    subgraph Worker["worker container"]
        W[Ingestion worker<br/>FOR UPDATE SKIP LOCKED] --> PARSE[Parsers + chunker]
        PARSE --> EMB[bge-m3 embeddings]
        SYNC[Connector sync<br/>folder / web, scheduler] --> UP[Normal upload + versioning]
    end
    SYNC -- "web: via SSRF guard" --> WEB[(Web pages)]
    SYNC -- "read-only mount" --> FS[(Connector roots)]
    API -- "upload: blob + job" --> BLOB[(Blob volume)]
    UP --> BLOB
    W --> BLOB
    EMB --> OLLAMA
    W --> PG[(PostgreSQL 16 + pgvector<br/>users, ACL, docs, chunks, jobs,<br/>secrets, connectors, registry,<br/>conversations, audit)]
    ORCH -- "messages, audit" --> PG
```

## Request flow (`POST /api/chat`)

1. Session cookie and CSRF header are verified; the user is loaded.
2. `effective_permissions(user)`: union of grants over the user's roles, computed from Postgres (readable containers, readable restricted documents, executable tools). Admin status adds no data access (ADR 0009).
3. Retrieval: the question is embedded locally; pgvector search runs with the ACL filter inside the SQL query (and only over current document versions); each hit is re-checked against the documents table; a similarity floor drops noise.
4. History: earlier answers in the conversation whose cited documents are no longer readable are withheld.
5. Tools: only tools with an `execute` grant are offered to the model.
6. Routing: effective classification = max(context, history). Confidential ⇒ local models only; if no permitted model exists, the request is refused before anything is sent. The user's chosen model, then the admin's default model, is used only if cleared for that classification; otherwise a cleared model (local first). With no authorized context and no tools, no model is called at all.
7. Generation streams over SSE. On a tool call: arguments are validated (strict schema, no reserved names, resource checks), the gateway mints a 60-second token scoped from the same permissions, the MCP server enforces the scope again, the validator tags and scrubs the result, and the router re-checks the model choice before the result is sent to any model.
8. The answer, citations, model and classification are stored; audit entries record retrieved chunk IDs, dropped chunks, withheld history, every LLM request (destination + classification) and every tool decision.

## Ingestion

Upload ⇒ SHA-256 ⇒ duplicate check (same container) ⇒ blob stored content-addressed ⇒ `document_versions` + `ingestion_jobs` row. The worker claims jobs with `FOR UPDATE SKIP LOCKED`, parses, chunks, embeds, inserts chunks and switches `documents.current_version_id` in one transaction, deleting the previous version's chunks of that document only.

## Storage

One PostgreSQL holds identities, ACL, documents, vectors, jobs, conversations and audit. Blobs live on a volume behind `BlobStore`; vectors behind `VectorStore` (pgvector). Both are interfaces so that S3 or Qdrant can be added later (ADR 0005).

## v0.2: administration and extensibility

- **Permissions** gain a `contribute` level between `read` and `manage` on containers (ADR 0017). Managers and contributors work on content through the same documents API; grants, roles, users and classification remain admin-only.
- **Credential store** (`secrets` table, ADR 0018): values encrypted with AES-256-GCM, the name as associated data, the master key in `ERAG_SECRETS_KEY(_FILE)`. Only internal components decrypt, each with a consumer label written to the audit log. The model router reads the OpenRouter key from it (env var as fallback) and is rebuilt when the key or the provider settings change.
- **MFA and step-up** (ADR 0019): sessions carry `mfa_pending` and `reauth_at`; sensitive endpoints depend on `require_step_up`.
- **Connectors** (ADR 0020): `connectors`, `connector_items`, `sync_runs`. The worker claims queued runs, lists items through the connector kind, and pushes new or changed items through the normal upload/versioning path, so ingestion, ACLs and routing are unchanged. Outbound HTTP goes through `app/core/netguard.py`.
- **MCP registry** (ADR 0021): registered servers are `pending` until approved; tools are discovered disabled; enabling pins the definition hash, which the gateway re-checks in the same session before every call. Each server has a clearance (max classification sent) and a result label (classification of everything returned).
- **Isolated runner** (ADR 0022): a separate service with the Docker socket. It starts only images pinned by ID, with network `none`, a read-only root filesystem, no capabilities, no-new-privileges, the default seccomp profile, a non-root user and limits. The gateway reaches the server over a Unix socket in a per-MCP directory.

## Provider registry (after v0.2)

- **Providers and models in the database** (`llm_providers`, `llm_models`, ADR 0023). Two kinds: Ollama instances and OpenAI-compatible APIs. The built-in `ollama` and `openrouter` rows are bootstrapped from the environment on start-up; the dev `claude-code` provider stays env-only.
- **Router build** (`app/llm/registry.py`, `load_router`): for each enabled provider the host is resolved and the locality derived (local only if every address is private), the clearance capped by locality and `ERAG_CLOUD_MAX_CLASSIFICATION`, the key read from the credential store, and one `OpenAICompatProvider` created per enabled model. The admin's default model is passed to `ModelRouter`, which uses it only when it is cleared. The router is cached and rebuilt when providers, models, secrets or settings change.
- **Every request** goes through `check_clearance`, the egress hook and a destination guard that re-resolves the host, applies the provider's address policy (private only for Ollama/self-hosted; a local provider must stay private), pins the connection to the checked address and never follows redirects (ADR 0025). Metered providers (every API provider and any non-local Ollama) are checked against a monthly ceiling and a daily request cap before the request is sent (ADR 0024).

# Architecture

English rendering of the original architecture diagram, updated with the design deltas of [PLAN.md section 2](PLAN.md). Decisions are in [`adr/`](adr/README.md).

```mermaid
flowchart LR
    U[Browser: React SPA] -- "HttpOnly session cookie + CSRF" --> API

    subgraph App["app container (FastAPI)"]
        API[API routers] --> ORCH[Chat orchestrator]
        ORCH --> AUTHZ["authz.effective_permissions()<br/>(from Postgres, every request)"]
        ORCH --> RET[Retriever]
        RET -- "query vector + ACL filter in SQL" --> PG
        RET --> RECHECK[ACL re-check]
        RECHECK --> CTX[Context builder<br/>dedupe, budget, citations]
        CTX --> ROUTER[Classification router<br/>max(context, history, tool results)]
        ROUTER --> PROMPT[Prompt builder<br/>untrusted data delimited]
        PROMPT --> LLM[OpenAI-compatible client<br/>+ egress hook]
        ORCH --> TOOLS[Tool runtime]
        TOOLS --> TAUTHZ[Tool authz<br/>grant, schema, resource]
        TAUTHZ --> GW[MCP gateway<br/>allowlist, rate limit,<br/>signed permission context]
        GW --> VAL[Result validator<br/>size, classification, injection]
        VAL --> ROUTER
    end

    LLM -- "any classification" --> OLLAMA[(Ollama, local<br/>qwen3:4b-instruct, bge-m3)]
    LLM -- "public / internal only" --> OR[(OpenRouter, cloud)]
    GW -- "stdio + _meta token" --> FILES[files_server]
    GW -- "stdio + _meta token" --> TICKETS[tickets_server<br/>SQLite]

    subgraph Worker["worker container"]
        W[Ingestion worker<br/>FOR UPDATE SKIP LOCKED] --> PARSE[Parsers + chunker]
        PARSE --> EMB[bge-m3 embeddings]
    end
    API -- "upload: blob + job" --> BLOB[(Blob volume)]
    W --> BLOB
    EMB --> OLLAMA
    W --> PG[(PostgreSQL 16 + pgvector<br/>users, ACL, docs, chunks,<br/>jobs, conversations, audit)]
    ORCH -- "messages, audit" --> PG
```

## Request flow (`POST /api/chat`)

1. Session cookie and CSRF header are verified; the user is loaded.
2. `effective_permissions(user)`: union of grants over the user's roles, computed from Postgres (readable containers, readable restricted documents, executable tools). Admin status adds no data access (ADR 0009).
3. Retrieval: the question is embedded locally; pgvector search runs with the ACL filter inside the SQL query (and only over current document versions); each hit is re-checked against the documents table; a similarity floor drops noise.
4. History: earlier answers in the conversation whose cited documents are no longer readable are withheld.
5. Tools: only tools with an `execute` grant are offered to the model.
6. Routing: effective classification = max(context, history). Confidential ⇒ local models only; if no permitted model exists, the request is refused before anything is sent. With no authorized context and no tools, no model is called at all.
7. Generation streams over SSE. On a tool call: arguments are validated (strict schema, no reserved names, resource checks), the gateway mints a 60-second token scoped from the same permissions, the MCP server enforces the scope again, the validator tags and scrubs the result, and the router re-checks the model choice before the result is sent to any model.
8. The answer, citations, model and classification are stored; audit entries record retrieved chunk IDs, dropped chunks, withheld history, every LLM request (destination + classification) and every tool decision.

## Ingestion

Upload ⇒ SHA-256 ⇒ duplicate check (same container) ⇒ blob stored content-addressed ⇒ `document_versions` + `ingestion_jobs` row. The worker claims jobs with `FOR UPDATE SKIP LOCKED`, parses, chunks, embeds, inserts chunks and switches `documents.current_version_id` in one transaction, deleting the previous version's chunks of that document only.

## Storage

One PostgreSQL holds identities, ACL, documents, vectors, jobs, conversations and audit. Blobs live on a volume behind `BlobStore`; vectors behind `VectorStore` (pgvector). Both are interfaces so that S3 or Qdrant can be added later (ADR 0005).

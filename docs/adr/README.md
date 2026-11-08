# Architecture Decision Records

Short records of decisions taken while building the MVP. Format: context, decision, consequences.
Decisions that the owner may want to revisit are marked **Owner review**.

| # | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions; stack from PLAN.md | Accepted |
| [0002](0002-query-time-acl.md) | ACL resolved at query time and pushed into the vector query | Accepted |
| [0003](0003-classification-routing-after-retrieval.md) | Classification routing after retrieval (and after every tool result) | Accepted |
| [0004](0004-async-sqlalchemy-asyncpg.md) | Async SQLAlchemy with asyncpg (no psycopg) | Accepted |
| [0005](0005-storage-interfaces.md) | `VectorStore` and `BlobStore` interfaces | Accepted |
| [0006](0006-ollama-on-host.md) | Ollama on the host by default; bundled Ollama as a profile | Accepted, Owner review |
| [0007](0007-local-chat-model.md) | Local chat model: `qwen3:4b-instruct-2507-q4_K_M` | Accepted (amended) |
| [0008](0008-testing-without-fake-providers.md) | Testing without fake providers | Accepted |
| [0009](0009-admin-is-not-data-access.md) | Admin is a management privilege, not a data-access privilege | Accepted, Owner review |
| [0010](0010-mcp-stdio-and-permission-context.md) | MCP over stdio with a signed permission context in `_meta` | Accepted |
| [0011](0011-own-openai-compatible-client.md) | Own minimal OpenAI-compatible client over httpx | Accepted |
| [0012](0012-tool-scope-from-container-permissions.md) | Tool data scope derived from container permissions | Accepted |
| [0013](0013-result-validator-heuristics.md) | Result validator: size cap, classification floor, injection heuristics | Accepted, Owner review |
| [0014](0014-frontend.md) | Frontend: small React SPA with vendored shadcn-style components | Accepted |
| [0015](0015-demo-bootstrap-and-hardening.md) | One-command seeded demo and MVP hardening | Accepted, Owner review |
| [0016](0016-openrouter-free-only-and-spend-ceiling.md) | OpenRouter: free models only and a hard monthly spend ceiling | Accepted |
| [0017](0017-delegated-content-management.md) | Delegated content management: `contribute` and `manage` on containers | Accepted |
| [0018](0018-credential-store.md) | Credential store: AES-256-GCM, master key outside the database, OpenRouter key in the vault | Accepted |
| [0019](0019-mfa-and-step-up.md) | Optional TOTP MFA and step-up re-authentication | Accepted |
| [0020](0020-connectors-and-ssrf.md) | Connectors: container-inherited permissions, folder roots, SSRF guard | Accepted |
| [0021](0021-mcp-registry-and-clearance.md) | MCP registry for remote servers and the clearance model | Accepted |
| [0022](0022-isolated-mcp-runner.md) | Isolated runner for MCP containers (network `none`, Unix-socket transport) | Accepted |

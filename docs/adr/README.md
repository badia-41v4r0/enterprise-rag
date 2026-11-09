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
| [0015](0015-demo-bootstrap-and-hardening.md) | One-command seeded demo and MVP hardening | Accepted, Owner review; compose default superseded by 0040 |
| [0016](0016-openrouter-free-only-and-spend-ceiling.md) | OpenRouter: free models only and a hard monthly spend ceiling | Superseded in part by 0028 |
| [0017](0017-delegated-content-management.md) | Delegated content management: `contribute` and `manage` on containers | Accepted |
| [0018](0018-credential-store.md) | Credential store: AES-256-GCM, master key outside the database, OpenRouter key in the vault | Accepted |
| [0019](0019-mfa-and-step-up.md) | Optional TOTP MFA and step-up re-authentication | Accepted |
| [0020](0020-connectors-and-ssrf.md) | Connectors: container-inherited permissions, folder roots, SSRF guard | Accepted |
| [0021](0021-mcp-registry-and-clearance.md) | MCP registry for remote servers and the clearance model | Accepted |
| [0022](0022-isolated-mcp-runner.md) | Isolated runner for MCP containers (network `none`, Unix-socket transport) | Accepted |
| [0023](0023-llm-provider-registry-and-default-model.md) | LLM provider registry: Ollama and OpenAI-compatible APIs, locality from the address, a default model | Accepted, Owner review |
| [0024](0024-per-provider-spend-ceiling-and-daily-request-cap.md) | Per-provider spend ceiling and daily request cap | Accepted, Owner review, amended by 0026 |
| [0025](0025-model-endpoint-egress-guard.md) | Egress guard for model endpoints: address classes, request-time re-validation, no redirects | Accepted |
| [0026](0026-cost-limits-token-accounting-preventive-check.md) | Cost limits, token accounting, prices and a preventive worst-case check; admins may raise limits | Accepted |
| [0027](0027-quotas-per-user-and-role.md) | Quotas per user and per role: standard caps, collective pools, overrides | Accepted, Owner review |
| [0028](0028-remove-openrouter-free-only-rule.md) | Remove the OpenRouter free-only rule | Accepted |
| [0029](0029-model-router.md) | Model router: permitted set first, deciders and lanes second, an instant kill switch | Accepted |
| [0030](0030-decision-protocol-and-decider-clearance.md) | One decision protocol for the official and open deciders; decider clearance and egress | Accepted |
| [0031](0031-open-decider-spike.md) | Open decider spike: Open-Jev 2B runs on this machine's CPU (about 1 minute per decision) | Accepted, Owner review |
| [0032](0032-official-jev-api-and-openrouter-jev-router.md) | Official Jev API decider and the OpenRouter Jev Router as a virtual model | Accepted, Owner review |
| [0033](0033-delete-users.md) | Deleting users: deactivate first, step-up, what is kept | Accepted |
| [0034](0034-setup-mode-and-setup-token.md) | Setup mode, the one-time setup token and `erag init` | Accepted, Owner review |
| [0035](0035-license-acceptance-record.md) | License acceptance in the wizard: a record and a deliberate pause | Accepted, Owner review (lawyer) |
| [0036](0036-installation-type-and-demo-removal.md) | Installation type: empty by default, the ACME demo on request, and removing it | Accepted |
| [0037](0037-forced-password-change.md) | Temporary passwords must be changed at the first login | Accepted |
| [0038](0038-environment-checks.md) | Environment checks: read-only, booleans and the exact fix, never values | Accepted |
| [0039](0039-explicit-ollama-downloads.md) | Ollama models are downloaded only on an explicit, confirmed administrator action | Accepted |
| [0040](0040-seed-demo-and-the-wizard.md) | `ERAG_SEED_DEMO` with the setup wizard | Accepted |
| [0041](0041-check-as-another-user.md) | "Does it work?": asking as another user, and what the administrator sees | Accepted, Owner review |

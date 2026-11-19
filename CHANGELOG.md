# Changelog

What changed in each version of EnterpriseRAG, and what you must do to upgrade. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/) (before 1.0, a minor version may change behaviour). The engineering log behind each version (what was verified, what was not) is in [`PROGRESS.md`](PROGRESS.md); decisions are in [`docs/adr/`](docs/adr/README.md).

**Upgrading in general:** back up the database and the master key, read the *Upgrade notes* of every version between yours and the target, then `alembic upgrade head` (Compose does it on start) and check `/health`.

## [Unreleased]

## [0.5.0] - not yet released (PR #12)

Many users in one company, hybrid search with a reranker and a permission-aware evaluation set, and scale and operations. Plan: [`docs/PLAN-v0.5.md`](docs/PLAN-v0.5.md).

### Added
- **Retrieval evaluation**: a permission-aware question set asked as personas, an independent leakage oracle, recall/MRR/nDCG and abstention per tag, recorded embeddings (no Ollama in CI) and a CI gate that fails on any leak or a quality drop (`erag eval ...`). Admin > Evaluation, thumbs up/down on answers, a review queue and a local faithfulness judge.
- **Hybrid search**: Postgres full text next to pgvector under one authorization clause, reciprocal rank fusion, and a **cross-encoder reranker inside the app process** with a degraded mode (`erag models install mmarco-minilm-l12`). Admin > Retrieval. On the evaluation set the reranker raises abstention on unanswerable questions from 0.31 to 0.96 (`docs/benchmarks/r14-retrieval.md`).
- **Many users**: paginated and searchable Admin > Users with filters, bulk actions and CSV import/export; a paginated, filterable audit log; idle session timeout and "Sign out everywhere"; conversation retention; usage per role (JSON/CSV).
- **Fair use across replicas**: rate limits shared through Postgres; **budget reservations** before every model call; a fair generation queue over **endpoint pools** (several model servers per provider, a maximum of concurrent generations each, role priority); query embeddings on their own endpoint.
- **Ingestion at scale**: job priorities, retries with backoff, dead letters with a Retry button, heartbeats and a reaper, several jobs per worker, back-pressure on connectors, parallel embeddings, a memory cap per file.
- **Storage**: an S3-compatible blob store (`ERAG_BLOB_BACKEND=s3`) and `erag blobs migrate --to s3`.
- **Operations**: Prometheus metrics on an internal port with alert rules (`deploy/monitoring/alerts.yml`), correlation ids in responses, logs, audit entries and traces, `/health/live` and `/health/ready`, `erag backup verify`, a restore drill script, a pgBackRest example, `erag maintenance`.
- **Deployment**: PgBouncer support, a Compose `scale` profile (two apps, two workers, PgBouncer), a Helm chart with a CloudNativePG example, graceful shutdown, a load test (`loadtest/`).

### Changed
- Permissions are cached per replica and invalidated in the same transaction as any permission change (revocation stays immediate). Users who can read little of the corpus get an exact search; others an index-driven one.
- Embeddings are stored as `halfvec` (half the size; identical results on the evaluation set).
- `ERAG_CONNECTOR_MAX_ITEMS` is now the default of each connector's own `max_items`, no longer a global ceiling.
- A parse failure (an unreadable file) fails the job at once instead of being retried.

### Fixed
- Concurrent requests could overspend a budget or a quota pool (budgets were checked, not reserved).
- The tokens of a request whose model failed mid-answer were not recorded.
- Sync runs left `running` by a crashed worker stayed stuck.

### Upgrade notes
- **Stop the app and the workers during the migration.** One migration changes the embedding column type (`vector` → `halfvec`) and rebuilds the vector index; the previous release cannot run against the new schema, and on a large corpus the rebuild takes time (raise `maintenance_work_mem` first).
- Optional: install the reranker (`erag models install mmarco-minilm-l12`); without it search uses the fused order and says so.
- New settings, all with defaults (see the README configuration reference): sessions (`ERAG_SESSION_IDLE_MINUTES`), retention, the generation queue, embedding endpoints, workers, S3, connection pools, metrics, traces.
- Point your monitoring at the metrics ports (9464 app, 9465 worker); they must not be public.
- After upgrading, run `erag backup verify`.

## [0.4.0] - 2026-11-09

Guided first-run setup for a company's administrator. Plan: [`docs/PLAN-v0.4.md`](docs/PLAN-v0.4.md). PR #10.

### Added
- `erag init` and `deploy/install.sh`: strong random secrets, a master key file, production settings, prerequisite checks; prints a one-time setup token.
- **Setup mode**: a database with no users serves only the setup wizard, behind a rate-limited one-time token; setup endpoints disappear once setup is completed. `erag setup --status | --new-token | --reset --yes` on the server.
- **A 12-step wizard**: license acceptance (recorded), environment checks with the exact fix, administrator with optional MFA, empty or demo installation (with "Remove demo data"), organization, models (explicit Ollama downloads), access structure, people one by one or by CSV with a forced password change, knowledge upload, "Does it work?", review with a security checklist and a setup report without secrets.
- After setup: a setup checklist card and Admin > Setup assistant.
- Installation guide and troubleshooting.

### Changed
- `ERAG_SEED_DEMO` defaults to `false`; `true` seeds the full demo and marks setup as completed.
- Every user created by an administrator must change the password at first login (demo personas excepted).

### Removed
- `ERAG_OPENROUTER_ALLOW_PAID` from the Compose file (ignored with a warning since 0.3.0).

### Upgrade notes
- **An existing installation is never put into setup mode**: a database that already has users is recorded as set up (migration and start-up check). Your data is not re-seeded.
- If you relied on the demo being seeded by default, set `ERAG_SEED_DEMO=true`.

## [0.3.1] - 2026-11-09

PR #8.

### Added
- Deleting users from Admin > Users (deactivate first, re-authentication required, never yourself): sessions, roles, personal quotas, MFA codes and conversations go; the audit log and token usage figures are kept.

## [0.3.0] - 2026-11-09

Cost limits and quotas, paid models, and a model router. Plan: [`docs/PLAN-v0.3.md`](docs/PLAN-v0.3.md). PR #7.

### Added
- **Token accounting** per provider, model, user and day; prices per model (synced from OpenRouter, entered by hand elsewhere); limits per provider and model (output tokens sent as `max_tokens`, input size, tokens per day and month, USD per month, requests per day, a per-turn cap across the tool loop), checked on the worst case before every request.
- **Quotas** per user and per role: a standard allowance per member, collective pools, personal overrides. Users see their own usage.
- **Model router** (off by default): chooses among the models a request may already use, with deciders (a prompted registry model, an open Jev-style server, the official Jev API) and lanes; dry run and a decision log without questions. The OpenRouter Jev Router as a virtual model.

### Changed
- Admins may **raise** limits as well as lower them (re-authentication, change history). The environment values are initial defaults, not ceilings.

### Removed
- The OpenRouter free-only rule and `ERAG_OPENROUTER_ALLOW_PAID` (ignored with a warning). OpenRouter is bounded by the same budgets as any paid provider.

### Upgrade notes
- **Set a credit limit on every paid provider's API key itself**: since limits can be raised from the panel, that is the bound that holds even if an admin session is hijacked.
- Unknown prices count as 0 in the worst-case check and are flagged in the panel: enter them.

## [0.2.1] - 2026-11-08

PRs #3, #4, #5.

### Added
- **Model providers from the admin panel**: Ollama instances and any OpenAI-compatible API, connection test, model discovery, a default model for the chat. Locality is derived from the resolved address (only private networks are local), clearance is capped by locality, keys live in the credential store, each request's destination is re-checked and pinned (no redirects, HTTPS for public endpoints), and paid APIs get a monthly ceiling and a daily request cap.
- White paper: credential protection, its limits and hardening options.

### Changed
- Admin shows one badge per classification with a count instead of one per container.

### Upgrade notes
- The built-in `ollama` and `openrouter` providers are created from the environment on first start; nothing to do.

## [0.2.0] - 2026-11-08

Administration and extensibility. Plan: [`docs/PLAN-v0.2.md`](docs/PLAN-v0.2.md). PR #2.

### Added
- **Roles and delegated management**: role templates, clone, bulk membership, a `contribute` level; container managers upload, replace and delete documents. Uploads UI with versions, replace and retry.
- **Credential store** (AES-256-GCM, master key outside the database, write-only, every use audited; `erag secrets ...`), optional **TOTP MFA** with recovery codes, **step-up** re-authentication for sensitive admin actions.
- **Connectors**: `folder` and `web` (SSRF-guarded), dry-run preview, schedules, run history; content inherits the container's permissions.
- **MCP registry** for remote servers (pending until approved, tools disabled until enabled, definition hashing against rug pulls, per-server clearance and result label) and an **isolated runner** for MCP containers (pinned images, no network, read-only, no capabilities).

### Fixed
- The `contribute` grant action existed in code without its migration.

### Upgrade notes
- **Production needs a master key** (`ERAG_SECRETS_KEY` or `ERAG_SECRETS_KEY_FILE`, created with `erag secrets init`). Back it up apart from the database: losing it loses every secret and MFA enrolment.
- Move the OpenRouter key into the credential store: `erag secrets set openrouter_api_key --from-env ERAG_OPENROUTER_API_KEY`, then remove the variable.

## [0.1.0] - 2026-11-08

The MVP: permission-aware RAG and an MCP gateway. Plan: [`docs/PLAN.md`](docs/PLAN.md).

### Added
- Identity and permissions: server-side sessions (HttpOnly cookie, CSRF), roles, containers classified `public`/`internal`/`confidential`, allow-only grants, restricted documents, per-tool grants, audit log.
- Ingestion: PDF, DOCX, XLSX, HTML, Markdown and text; SHA-256 dedupe, versioning, a Postgres job queue, local `bge-m3` embeddings.
- Authorized retrieval: permissions resolved from Postgres on every request and applied inside the pgvector query, then re-checked.
- Classification routing: confidential data only goes to local models; OpenRouter optional, free models only, with a spend ceiling.
- MCP gateway with tool authorization, a signed permission context and a result validator; `files` and `tickets` reference servers.
- React chat with streaming answers and citations, admin panel; the seeded ACME Corp demo and a security test suite (canary leakage matrix, revocation, routing, tools).

[Unreleased]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.4.0...HEAD
[0.5.0]: https://github.com/badia-41v4r0/enterprise-rag/pull/12
[0.4.0]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/badia-41v4r0/enterprise-rag/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/badia-41v4r0/enterprise-rag/releases/tag/v0.1.0

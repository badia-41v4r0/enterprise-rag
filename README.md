# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://badiapartners.com).

> **Status: v0.5 in development** (on top of v0.3). Runnable end to end with a seeded demo company. v0.5 adds many users in one company, hybrid search with a reranker and a permission-aware evaluation set, and scale and operations (R13 to R19 built; R20, the Helm chart and load tests, in progress). Progress and verification notes: [`PROGRESS.md`](PROGRESS.md). Plans: [`docs/PLAN.md`](docs/PLAN.md) (MVP), [`docs/PLAN-v0.2.md`](docs/PLAN-v0.2.md) (administration, connectors, MCP extensibility), [`docs/PLAN-v0.3.md`](docs/PLAN-v0.3.md) (cost limits and quotas, paid models, model router), [`docs/PLAN-v0.4.md`](docs/PLAN-v0.4.md) (guided first-run setup, planned), [`docs/PLAN-v0.5.md`](docs/PLAN-v0.5.md).

## The idea

Most enterprise RAG and agent setups let the model see (or reach) more than the person asking is allowed to see. EnterpriseRAG is built around a few principles:

1. The model never decides permissions; the backend does.
2. Access control is applied during retrieval, before the LLM sees anything.
3. Every tool call goes through a gateway with authorization and audit, and tools only receive data the user is already allowed to see.
4. Data classification decides which models (local or cloud) and which tools may process it.
5. Secrets never reach the browser or the model.
6. The system can be extended (content managers, connectors, new MCP servers) without giving up 1 to 5.

## What it does

- **Identity and permissions:** server-side sessions (HttpOnly cookie + CSRF), roles, containers classified `public / internal / confidential`, allow-only grants, restricted documents, per-tool `execute` grants, audit log. Admin is a management privilege, not data access.
- **Roles and delegated management:** role templates (Reader, Contributor, Manager), rename, clone, bulk membership, a grants matrix and an effective-permissions preview. A department head with `manage` on their container uploads, replaces and deletes its documents; `contribute` uploads and replaces only. Neither can change who has access or the classification.
- **Uploads UI:** drag and drop, per-file status, version history, replace, retry failed ingestion, delete.
- **Credential store:** connector, MCP and model-provider secrets encrypted at rest (AES-256-GCM, master key outside the database), write-only, every use audited.
- **Users:** an admin creates, edits, deactivates and, once deactivated, **deletes** users (Admin > Users; re-authentication required). Deleting removes the person's sessions, roles, personal quotas and conversations; the audit log and the token usage figures are kept ([ADR 0033](docs/adr/0033-delete-users.md)).
- **MFA and step-up:** optional TOTP for every user with recovery codes; re-authentication within 5 minutes for sensitive admin actions (secrets, providers and the default model, connectors, MCP registration, admin promotion, MFA resets).
- **Connectors:** `folder` (below allowed roots, no symlink escapes) and `web` (URLs or a sitemap, SSRF-guarded) sync into one container and inherit its permissions; scheduled or manual, with dry-run preview and run history.
- **Ingestion:** PDF, DOCX, XLSX, HTML, Markdown and text; SHA-256 dedupe, versioning, local `bge-m3` embeddings. A Postgres-backed job queue with priorities (an upload before a connector sync), retries with backoff, dead letters with a Retry button, a heartbeat and a reaper, several jobs per worker and back-pressure on connectors. Blobs on local disk or any S3-compatible store.
- **Authorized retrieval:** the user's permissions are resolved from PostgreSQL (cached per replica and invalidated in the same transaction as any change, so revocation stays immediate) and pushed into the search as a filter, then re-checked.
- **Hybrid search with a reranker:** pgvector and Postgres full text under one authorization clause, reciprocal rank fusion, and a cross-encoder reranker inside the app process with a degraded mode ([ADR 0038](docs/adr/0038-hybrid-search-and-rank-fusion.md), [ADR 0039](docs/adr/0039-reranker-in-process.md)). Settings in Admin > Retrieval. Users who read little of the corpus get an exact search; everyone else an index-driven one.
- **Many users in one company:** paginated and searchable user admin with filters, bulk actions and CSV import and export; a paginated, filterable audit log; idle and absolute session lifetimes and "sign out everywhere"; conversation retention; usage reports per role.
- **Fair use across replicas:** rate limits shared through Postgres, budgets **reserved** before each model call (never overspent by concurrent requests), and a fair generation queue over pools of model endpoints: a free slot goes to the user with the fewest requests in flight, and the chat shows the queue position ([Fair use and capacity](#fair-use-and-capacity)).
- **Operations:** stateless app replicas, PgBouncer support, Prometheus metrics with security alerts, correlation ids in logs, audit and traces, liveness and readiness, `erag backup verify` and a restore drill ([Operations](#operations-scaling-metrics-and-backups)).
- **Retrieval evaluation:** a permission-aware question set asked as personas (expected documents, unanswerable questions, documents a persona must never get), an independent leakage oracle, recall/MRR/nDCG per tag, and a CI gate that fails on any leak or a quality drop ([Evaluation](#evaluation), [ADR 0040](docs/adr/0040-permission-aware-evaluation.md)).
- **Classification routing:** after retrieval (and after every tool result) the effective classification decides which models may answer. Confidential data only ever goes to local models. OpenRouter is optional and bounded by the same budgets as any paid provider.
- **Model providers from the admin panel:** add Ollama instances and any OpenAI-compatible API (OpenAI, Together, Groq, vLLM, LM Studio, ...), test the connection, discover models, and choose the default model for the chat. Locality is derived from the address (only private networks are local), clearance is capped by locality, keys live in the credential store, every provider is re-checked on every request, and paid APIs have a monthly ceiling and a daily request cap.
- **Cost limits and quotas:** token accounting per provider, model and user; a price table (synced from OpenRouter, entered by hand elsewhere); limits per provider and model (output tokens per request sent as `max_tokens`, input size, tokens per day and month, USD per month, requests per day, a per-turn cap across the tool loop) checked on the worst case **before** a request is sent; quotas per user and per role (a standard allowance per member or a collective pool); admins may raise or lower limits with step-up, and every change is in a change history. Users see their own usage; admins see aggregates and per-user figures, never prompts or answers.
- **Model router** (off by default): chooses, per question, among the models the request is already allowed to use, with a decider (a prompted registry model, an open Jev-style decision model you run, or TypeSafe's hosted Jev API) and lanes; it can never widen the permitted set, falls back to the default model and the existing rules on any failure, and logs decisions without the question.
- **MCP:** a gateway with tool authorization, a signed permission context for the built-in servers, a result validator; a **registry** for remote MCP servers (tools discovered disabled, approved one by one, per-role grants, argument constraints, rug-pull detection, a clearance per server and a label on everything it returns); and an **isolated runner** that runs approved MCP images with no network, a read-only filesystem and no capabilities.
- **Frontend:** React chat with streaming answers and clickable citations, a Content page for managers, an Account page (MFA), and an admin panel.
- **ACME Corp demo** and a security test suite (canary leakage matrix, revocation, routing, tools, delegation, vault/MFA, connectors/SSRF, MCP registry, runner confinement). See [`demo/acme/README.md`](demo/acme/README.md).

## Quick start (demo)

Requirements: Docker with Compose v2, and [Ollama](https://ollama.com) with two models:

```bash
ollama pull bge-m3      # embeddings (1.2 GB)
ollama pull qwen3:4b-instruct-2507-q4_K_M    # local chat model (2.5 GB)
```

Then, from the repo root:

```bash
# Optional, for the isolated MCP runner: build the demo MCP image and allow its exact image ID.
docker build -f deploy/mcp-office.Dockerfile -t erag-mcp-office:0.2 .
export ERAG_RUNNER_IMAGES="office=$(docker image inspect erag-mcp-office:0.2 --format '{{.Id}}')"

# Ollama on the host listening on 127.0.0.1 (default install): share the host network
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build

# or, if your Ollama listens on 0.0.0.0 (OLLAMA_HOST=0.0.0.0):
docker compose up -d --build
```

Open http://127.0.0.1:8000 and log in as `hr@acme.test`, `engineer@acme.test`, `finance@acme.test`, `newhire@acme.test`, `exec@acme.test`, `hr.assistant@acme.test` or `admin@acme.test` (password `acme-demo-password`). The demo is seeded on start-up: documents, a folder connector over an engineering wiki, and a demo remote MCP server (`acme-office`) registered as *pending* for the admin to review. Then run the scripted scenarios:

```bash
uv run python scripts/demo_scenarios.py --base http://127.0.0.1:8000
```

Optionally install the reranker (129 MB, pinned by hash; nothing downloads by itself). Without it search uses the fused order and says so:

```bash
docker compose -f deploy/docker-compose.yml exec app erag models install mmarco-minilm-l12
```

Set `APP_PORT` if port 8000 is taken. A bundled Ollama container exists behind `--profile ollama` (NVIDIA GPU via CDI; not verified, see [ADR 0006](docs/adr/0006-ollama-on-host.md)). The demo secrets and password are for local use only: see [Production settings](#production-settings).

### Optional: OpenRouter

Set the key in **Admin > Providers**: it is stored encrypted in the credential store and can be changed without a restart. `ERAG_OPENROUTER_API_KEY` (in `deploy/.env` for Docker Compose, or `.env` at the repo root) is only a bootstrap fallback used while the store has no key; to move an existing key into the store run `erag secrets set openrouter_api_key --from-env ERAG_OPENROUTER_API_KEY` and then remove the variable. Any OpenRouter model may be added; the default one is free so the demo costs nothing. Spend is bounded like any paid provider (see [Cost limits and quotas](#cost-limits-and-quotas)): a monthly ceiling (`ERAG_OPENROUTER_MONTHLY_BUDGET_USD`, default `0.01` USD), prices synced from OpenRouter's model list (**Sync prices**) for the worst-case check before every request, a daily request cap, `max_tokens` and quotas ([ADR 0028](docs/adr/0028-remove-openrouter-free-only-rule.md), [ADR 0018](docs/adr/0018-credential-store.md)). **Also set a credit limit on the OpenRouter key itself**: it is the bound that holds even if the application is misconfigured. `ERAG_OPENROUTER_ALLOW_PAID` no longer exists and is ignored with a warning. Confidential data never goes to the cloud; what else may go is controlled by `ERAG_CLOUD_MAX_CLASSIFICATION`.

### Model providers and the default model

**Admin > Providers** lists every chat model provider with its locality, clearance, status, where its key comes from (credential store or environment, never the key itself), spend this month and requests today. The built-in `ollama` and `openrouter` providers are created from the environment on start-up, so an existing installation keeps working with no admin action.

To add a provider (requires re-entering your password, or a TOTP code with MFA):

1. Choose the kind. **Ollama**: the base URL of an Ollama instance (for example `http://192.168.1.20:11434`). **API**: the base URL of any OpenAI-compatible endpoint including its version path (for example `https://api.groq.com/openai/v1`, `https://api.together.xyz/v1`, or a self-hosted vLLM/LM Studio at `http://10.0.0.5:8000/v1` with **self-hosted** ticked). Native Anthropic or Gemini APIs are not supported (use them through an OpenAI-compatible gateway).
2. Optionally enter an API key: it is stored encrypted in the credential store as `provider.<slug>.api_key` and is never shown again.
3. Save, then **Test** (a read-only request for the model list) and **Discover models** (`/api/tags` for Ollama, `/models` for APIs), and add the models you want; you can also type a model id by hand. Model ids are `<provider slug>:<model>`.

What the software decides for you (ADR 0023 to 0025):

- **Locality** comes from the address the URL resolves to: loopback, private, link-local or CGNAT addresses are *local*; any public address is *cloud* (also for Ollama). Private addresses are only accepted for Ollama or self-hosted API providers; cloud metadata and other special addresses are always refused; public endpoints must use HTTPS. The address is checked again on every request and the connection is pinned to the checked address.
- **Clearance**: local providers may receive up to confidential data; cloud providers at most `ERAG_CLOUD_MAX_CLASSIFICATION` (default `internal`, never confidential). New cloud providers start cleared for `public` data only; you may raise them up to that cap.
- **Spend**: every API provider and any non-local Ollama has a monthly ceiling (`ERAG_PROVIDER_MONTHLY_BUDGET_USD`, default 0.01 USD; OpenRouter `ERAG_OPENROUTER_MONTHLY_BUDGET_USD`) and a daily request cap (`ERAG_PROVIDER_DAILY_REQUEST_CAP`, default 100), checked before each request on the worst case of the request. These are defaults: the panel can lower or raise them (see below).

**Default model.** In *Default model* (or *Make default* next to a model) choose the model the chat uses when the user picks none. It is used only when it is cleared for the request: a question grounded in confidential documents is still answered by a local model even if the default is a cloud model. *Automatic* keeps the previous behaviour (prefer local models). The default model and its provider cannot be deleted or disabled until another default is chosen. The chat model picker marks the default and shows each model's locality and clearance.

### Cost limits and quotas

**Admin > Providers**, on each provider card: usage bars for requests today, USD this month and tokens today and this month (amber from 80 percent, red with "limit reached"), a 30-day chart, and **Edit and limits**: monthly ceiling, requests per day, maximum output tokens per request (sent as `max_tokens`, default `ERAG_PROVIDER_MAX_OUTPUT_TOKENS` = 2048), maximum input tokens per request, tokens per day and per month, and a per-turn token cap across the tool loop. **Price and limits** on each model: price in USD per million input and output tokens (OpenRouter: **Sync prices** fills them from its model list; local Ollama models cost 0; other APIs: enter them, an unknown price is flagged) and per-model caps. Before every request the worst case (estimated input tokens x input price + max output tokens x output price) must fit in what remains of every applicable budget, otherwise nothing is sent and the chat says which limit was reached ([ADR 0026](docs/adr/0026-cost-limits-token-accounting-preventive-check.md)).

**Raising limits.** The environment values are initial defaults, not ceilings: an admin may lower or raise any limit, price or quota, always with step-up re-authentication, and every change (who, when, old and new value) appears in **Change history** at the bottom of the Providers screen. Because a hijacked admin session could raise budgets, also set a credit limit on each provider's own API key.

**Quotas** ([ADR 0027](docs/adr/0027-quotas-per-user-and-role.md)), in tokens (input plus output) and estimated USD, per UTC day and month, for all providers or one provider:

- **Admin > Roles > Quotas per role**: a *standard cap* gives each member the same allowance, counted per person; a *collective pool* is one allowance shared by everyone with the role (for example one pool for all of Engineering).
- **Admin > Users > Personal quota and usage**: a personal override, which replaces the role standard caps for that person, and that person's usage.
- How they combine: a person's allowance is their override if any, otherwise the highest standard cap among their roles; every collective pool of every role they hold is charged; a request is refused if any applicable cap is used up. The user then sees "Your daily allowance is used up; it resets at 00:00 UTC."
- Users see their own usage on the **Account** page (for a shared pool, only whether it has room). Admins see aggregates and per-user figures under Roles; prompts and answers are never stored with usage.

### Model router

**Admin > Model router** (off by default; off means exactly the previous behaviour; **Disable** is an instant kill switch, enabling and every change need re-authentication). Classification first, optimization second: for each question the router computes the *permitted* models (cleared for the request's classification, enabled, within budgets and quotas, tool-capable when tools are offered), asks a **decider** four questions (task type, difficulty 1 to 5, needs tools, needs long context) and sends the question to the first permitted target of the first matching **lane**; otherwise the default lane, the default model, then the existing rules. A question where the user picked a model explicitly is not routed. See [ADR 0029](docs/adr/0029-model-router.md) and [ADR 0030](docs/adr/0030-decision-protocol-and-decider-clearance.md).

- **Deciders**, tried in order (the built-in deterministic **rules** decider is always last):
  - **Prompted LLM**: any model of the provider registry (for example the local `qwen3:4b-instruct` model) answers in strict JSON; its clearance and budgets are those of its model. The practical choice on modest hardware.
  - **Open Jev-style server**: a decision model you run that speaks `POST /v1/systemone` (for example Open-Jev, Apache-2.0 adapter and MIT code; see [Open decider](#open-decider-optional) below). Tick **self-hosted** for a private address.
  - **Official Jev API** (TypeSafe): endpoint `https://api.typesafe.ai/v1/systemone` and model `jev-latest` by default (both editable), key in the credential store.
- **Clearance**: a decider outside your network starts cleared for `public` data and can never be cleared for confidential; it is only called when the request's classification is within its clearance (otherwise the next decider or the rules decide). Deciders receive the question and metadata only (classification label, tools offered, context size), never the retrieved documents, unless the decider is local and **see context** is on. Each decider has its own daily request cap and monthly budget (defaults 100 requests and 0.01 USD for non-local deciders).
- **Lanes**: conditions (task types, difficulty range, needs tools, needs long context, classifications) and ordered target models, plus one default lane. Example: easy internal questions to a small local model, hard public questions to a stronger cloud model, tool use to a tool-capable model.
- **Dry run**: type a sample question, choose a classification and a persona, and see the permitted set, the decider's answers, the chosen model and the reason, without generating an answer.
- **Decision log**: time, classification, permitted set, decider attempts, answers, chosen model, reason, latency and cost. Never the question or any context.

#### Open decider (optional)

Any server speaking `POST /v1/systemone` works. Tested on this project's reference machine with [Open-Jev 2B](https://huggingface.co/ZefanCai/Open-Jev-2B) (adapter Apache-2.0, code MIT, base `Qwen/Qwen3.5-2B` Apache-2.0), on CPU, about 3.3 GB of RAM and **about 1 minute per decision** with `--prefix-cache` (a GPU with 8 GB or more is much faster). Install it in its own environment, never in this project's:

```bash
mkdir -p ~/.cache/erag-open-jev && cd ~/.cache/erag-open-jev
git clone https://github.com/Zefan-Cai/Open-Jev.git src
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python --index-url https://download.pytorch.org/whl/cpu "torch>=2.8"
uv pip install -p .venv/bin/python "transformers==5.10.2" "peft==0.19.1" "accelerate==1.13.0" safetensors "huggingface_hub[cli]"
uv pip install -p .venv/bin/python --no-deps -e ./src
.venv/bin/hf download ZefanCai/Open-Jev-2B --revision 0c7aa498b1627be8da4acf34c863ff0ee0a92785 --local-dir models/Open-Jev-2B
.venv/bin/hf download Qwen/Qwen3.5-2B --revision 15852e8c16360a2fea060d615a32b45270f8a8fc
# Port 8791 is used by the demo MCP server, so use another one. Cap its memory so it can never take the host down.
HF_HUB_OFFLINE=1 OMP_NUM_THREADS=6 systemd-run --user --scope -p MemoryMax=6500M \
  .venv/bin/python -m jev.server --checkpoint models/Open-Jev-2B/package/checkpoint \
  --device cpu --max-length 4096 --batch-size 1 --prefix-cache --host 127.0.0.1 --port 8792
```

Then in Admin > Model router add a decider of kind **Open Jev-style server**, URL `http://127.0.0.1:8792`, **self-hosted** ticked, timeout 120000 ms; it is local, so it may be cleared up to confidential and may "see context". Opt-in test: `ERAG_TEST_OPEN_JEV_URL=http://127.0.0.1:8792 uv run pytest -m decider_open`. Spike details: [ADR 0031](docs/adr/0031-open-decider-spike.md).

#### Adding the official Jev key and testing it (about EUR 1)

1. Admin > Model router > **Add a decider**: kind **Official Jev API (TypeSafe)**, a name, paste the API key (stored encrypted, never shown again). Leave the endpoint and model empty for the defaults (`https://api.typesafe.ai/v1/systemone`, `jev-latest`), or enter the base URL and model name you were given (for a key issued through another gateway, use that gateway's `/v1/systemone` URL). It starts cleared for `public` data only, with a daily cap of 100 requests and a monthly budget of 0.01 USD; raise the budget to what you want to spend (for example 1.00) in the same form.
2. Press **Test**: one minimal real call with the sample question "What is the capital of France?", showing latency, input/output tokens, cost and the answers. The price used is a third-party figure (0.042 USD per million input tokens, output free; TypeSafe's reference only says output tokens are currently free): adjust it if your invoice says otherwise.
3. Put the decider first in the order, add a default lane, use **Dry run** with sample text, then **Enable router**. Every call is audited (`router.decider.request`) and counted against the decider's budget; the decision log shows each decision's cost.
4. Optional automated check: `ERAG_TEST_JEV_API_KEY=... uv run pytest -m decider_official` (a handful of real calls, skipped without the variable).

The official API was built from TypeSafe's reference and has not yet been called for real ([ADR 0032](docs/adr/0032-official-jev-api-and-openrouter-jev-router.md)); if the first **Test** fails, the error shows TypeSafe's message and the router keeps working with the next decider.

#### OpenRouter Jev Router (optional)

OpenRouter also offers the Jev Router as a model that both routes and serves (`typesafe/jev-router`). Add it by typing `typesafe/jev-router` in **Add model** on the OpenRouter card, then **Price and limits** to set its options (cost tier `low`/`medium`/`high`, allowed and excluded model patterns, sent as OpenRouter's `jev-router` plugin). It sees the whole conversation, so it is cleared for **public** data only by default (a model can only lower its provider's clearance). A cost tier is a preference, not a price cap: OpenRouter's monthly ceiling, the request cap and quotas bound it, and since its price is not published the worst-case check cannot price it, so keep the ceiling low and prefer an allowed list of models whose prices you know. Not called for real in this release.

### Users at company scale

**Admin > Users** searches by name or email and filters by role, status and "never logged in", one page of 50 at a time, with the total. Select users (or a whole page) to add or remove a role, activate, deactivate (their sessions end at once) or sign them out everywhere; you are never deactivated by your own bulk action. **Export CSV** downloads the filtered list (cells that would be spreadsheet formulas are neutralized). **Import users from CSV** takes `email,display_name,roles` (roles separated by `;`): **Check** validates the whole file first and shows what would happen; **Apply** (re-authentication required) creates new users with a temporary password shown once, and adds roles to existing users (never removes them). Nobody becomes an administrator through a file.

**Admin > Audit** pages through the log newest first and filters by action and resource prefix, decision and date range. **Usage by role** (Admin > Roles) gives requests, tokens and cost per role for any range of days, also as CSV.

Sessions end after `ERAG_SESSION_IDLE_MINUTES` without use (default 120) and at most `ERAG_SESSION_TTL_HOURS` after login (12). Each user can **Sign out everywhere** from the Account page. Conversations without activity for `ERAG_CONVERSATION_RETENTION_DAYS` are deleted (default 0: keep), audited as counts only.

### Fair use and capacity

- **Rate limits** (login, MFA, chat, tools) hold across every replica ([ADR 0035](docs/adr/0035-shared-limits-in-postgres.md)).
- **Budgets are reserved.** Before each model call the worst case is reserved on every budget and pool that applies, under locks taken in a fixed order, and settled at the real cost when the answer ends; a request that fails half-way still records what it used ([ADR 0036](docs/adr/0036-budget-reservations.md)).
- **Endpoint pools and the generation queue** ([ADR 0037](docs/adr/0037-endpoint-pools-and-fair-generation-queue.md)). Give a provider a **maximum concurrency** and extra **endpoints** (one Ollama or vLLM per GPU; Admin > Providers; each must have the provider's locality). When every slot is busy, requests wait: a slot goes first to roles with a higher **priority** (Admin > Roles), then to the user with the fewest generations in flight, then to the oldest request. The chat shows "waiting, position N"; after `ERAG_GENERATION_QUEUE_TIMEOUT_SECONDS` (120) the request stops with a clear message. Providers without a limit skip the queue entirely.
- **Embeddings:** chat questions can use their own endpoint (`ERAG_QUERY_EMBED_URL`) so a large ingestion never slows the chat; ingestion spreads over `ERAG_INGEST_EMBED_URLS`.
- Sizing: [`docs/operations/capacity.md`](docs/operations/capacity.md).

## Operations: scaling, metrics and backups

- **Scaling** ([ADR 0041](docs/adr/0041-horizontal-scaling-pgbouncer-and-advisory-locks.md)): app replicas and workers share everything through Postgres; singleton tasks (connector scheduler, maintenance) elect themselves with advisory locks. `docker compose --profile scale up -d` adds PgBouncer, a second app and a second worker. Behind PgBouncer (transaction mode) set `ERAG_DB_PGBOUNCER=true` and a direct `ERAG_DB_DIRECT_URL` for the queue's LISTEN.
- **Storage** ([ADR 0042](docs/adr/0042-s3-blob-store-and-vector-scaling-path.md)): `ERAG_BLOB_BACKEND=s3` with `ERAG_S3_*` for any S3-compatible store; `erag blobs migrate --to s3` moves an existing installation. Embeddings are stored as `halfvec` (half the size, the same retrieval quality on the evaluation set). **Upgrading to this release rebuilds the vector index: stop the app during the migration.**
- **Metrics and alerts** ([ADR 0044](docs/adr/0044-observability-without-content.md)): Prometheus metrics on an internal port (9464 app, 9465 worker), alert rules in [`deploy/monitoring/alerts.yml`](deploy/monitoring/alerts.yml), reference in [`docs/operations/metrics.md`](docs/operations/metrics.md). No metric, log field or span carries a question or document text. `/health/live` and `/health/ready` for probes. Every response has an `X-Request-ID` that is also in the logs, on the audit entries and on the traces.
- **Backups** ([ADR 0043](docs/adr/0043-backup-and-recovery.md), [`docs/operations/backup.md`](docs/operations/backup.md)): pgBackRest with point-in-time recovery (RPO 15 min, RTO 4 h), blobs in a versioned bucket, keys kept apart from the backups. After any restore run `erag backup verify` (schema, master key, blobs, embeddings); rehearse with `scripts/restore_drill.py`.

## Configuration reference (new in v0.2)

All settings use the `ERAG_` prefix (full list in [`backend/app/core/config.py`](backend/app/core/config.py)).

| Setting | Default | Purpose |
|---|---|---|
| `ERAG_SECRETS_KEY` / `ERAG_SECRETS_KEY_FILE` | dev: derived from `ERAG_SECRET_KEY`; Compose: `/data/secrets.key` (generated on first start) | Credential store master key (32 random bytes, base64). Create with `erag secrets init [--file F]`. **Required in prod.** Back it up outside the database: losing it loses all secrets and MFA enrolments. Rotate with `erag secrets rotate-master --new-key-file F`. |
| `ERAG_STEP_UP_SECONDS` | `300` | How recent a re-authentication must be for sensitive admin actions. |
| `ERAG_PROVIDER_MONTHLY_BUDGET_USD` | `0.01` | Default monthly spend ceiling per metered provider (USD): reported cost, else tokens x price; the worst case is checked before sending. The panel can lower or raise it (step-up). |
| `ERAG_PROVIDER_DAILY_REQUEST_CAP` | `100` | Default requests per day per metered provider (API providers, non-local Ollama, OpenRouter), counted before sending. The panel can lower or raise it (step-up). |
| `ERAG_PROVIDER_MAX_OUTPUT_TOKENS` | `2048` | Default maximum output tokens per request (sent as `max_tokens`), also the output side of the worst-case check. |
| `ERAG_OPENROUTER_MONTHLY_BUDGET_USD` | `0.01` | Default monthly ceiling for OpenRouter. (`ERAG_OPENROUTER_ALLOW_PAID` was removed in v0.3 and is ignored with a warning.) |
| `ERAG_CLOUD_MAX_CLASSIFICATION` | `internal` | Highest classification any cloud model may receive (`public` or `internal`; confidential never). |
| `ERAG_MFA_ISSUER`, `ERAG_MFA_MAX_ATTEMPTS` | `EnterpriseRAG`, `5` | TOTP issuer shown in authenticator apps; codes per pending login. |
| `ERAG_CONNECTOR_ROOTS` | none (Compose: `/connectors`) | Comma-separated folders that `folder` connectors may read below (mount them read-only into app and worker). |
| `ERAG_CONNECTOR_MAX_ITEMS`, `ERAG_CONNECTOR_MAX_ITEM_MB`, `ERAG_CONNECTOR_SCHEDULER_SECONDS` | `1000`, `25`, `30` | Connector limits and how often the worker checks schedules. |
| `ERAG_SSRF_ALLOWLIST` | none (Compose: the demo MCP server) | Non-public destinations that web connectors and remote MCP servers may reach: `host`, `host:port` or a CIDR. Deliberately not editable from the UI. |
| `ERAG_RUNNER_URL`, `ERAG_RUNNER_TOKEN` | unset (Compose: the `runner` service, a dev token) | Where the app reaches the isolated runner, and the shared token (strong value required in prod). |
| `ERAG_RUNNER_IMAGES` | none | Runner side: approved MCP images as `name=sha256:<image id>`, comma-separated. Nothing else can run. |
| `ERAG_RUNNER_SOCKET_DIR` | `/tmp/erag-runner-sockets` | Per-MCP Unix socket directories, shared by runner and app at the same path. |
| `ERAG_RUNNER_MOUNT_ROOTS` | none | Host folders an MCP may mount read-only. |
| `ERAG_RUNNER_IDLE_MINUTES`, `ERAG_RUNNER_MAX_CPUS`, `ERAG_RUNNER_MAX_MEMORY_MB`, `ERAG_RUNNER_DOCKER_SOCKET` | `15`, `2`, `2048`, `/var/run/docker.sock` | Idle shutdown, per-MCP resource ceilings, engine socket. |
| `ERAG_EVAL_DATABASE_URL`, `ERAG_BENCH_DATABASE_URL` | the app's server, databases `erag_eval` / `erag_bench` | Where `erag eval` and the benchmark scripts work. They refuse to run against the application database, because the evaluation corpus creates personas and confidential documents (ADR 0040). |
| `ERAG_SESSION_TTL_HOURS`, `ERAG_SESSION_IDLE_MINUTES` | `12`, `120` | Absolute session lifetime; idle timeout (0 turns it off). |
| `ERAG_CONVERSATION_RETENTION_DAYS` | `0` (keep) | Delete conversations with no activity for this many days. |
| `ERAG_MAINTENANCE_INTERVAL_MINUTES` | `15` | Expired sessions, retention, expired reservations and leases (one replica at a time). Also `erag maintenance`. |
| `ERAG_GENERATION_QUEUE_TIMEOUT_SECONDS` | `120` | How long a chat request waits for a model slot. |
| `ERAG_QUERY_EMBED_URL` | the Ollama URL | Embedding endpoint for chat questions (keep it free of bulk ingestion). |
| `ERAG_INGEST_EMBED_URLS`, `ERAG_INGEST_EMBED_CONCURRENCY`, `ERAG_EMBED_BATCH_SIZE` | the Ollama URL, `2`, `16` | Embedding pool for ingestion (comma-separated), requests in flight per worker, texts per request. |
| `ERAG_WORKER_CONCURRENCY`, `ERAG_JOB_MAX_ATTEMPTS`, `ERAG_JOB_RETRY_BASE_SECONDS`, `ERAG_JOB_LEASE_SECONDS` | `2`, `3`, `30`, `120` | Jobs in parallel per worker; attempts before a dead letter; first retry delay (doubles, at most 1 h); heartbeat lease of a running job or sync. |
| `ERAG_INGEST_QUEUE_MAX`, `ERAG_INGEST_MAX_BLOB_MB` | `5000`, `200` | Connectors stop enqueueing above this queue depth; files above this size are refused before being read. `ERAG_CONNECTOR_MAX_ITEMS` is now the default of each connector's own `max_items`, not a global ceiling. |
| `ERAG_BLOB_BACKEND`, `ERAG_S3_ENDPOINT`, `ERAG_S3_BUCKET`, `ERAG_S3_REGION`, `ERAG_S3_PREFIX`, `ERAG_S3_ACCESS_KEY_ID`, `ERAG_S3_SECRET_ACCESS_KEY`, `ERAG_S3_SSE` | `local`, ..., `us-east-1`, ..., `AES256` | Blob storage. `s3` works with any S3-compatible server (tested with SeaweedFS). `ERAG_S3_SSE` empty for a server that rejects the encryption header. |
| `ERAG_DB_POOL_SIZE`, `ERAG_DB_MAX_OVERFLOW`, `ERAG_DB_PGBOUNCER`, `ERAG_DB_DIRECT_URL` | `10`, `10`, `false`, the database URL | Connection pool per process; PgBouncer transaction-mode compatibility; a direct connection for LISTEN/NOTIFY. |
| `ERAG_METRICS_HOST`, `ERAG_METRICS_PORT` | `127.0.0.1`, `9464` (worker: +1) | Internal Prometheus endpoint (0 turns it off). Compose binds it inside the project network, unpublished. |
| `ERAG_OTEL_ENDPOINT`, `ERAG_OTEL_SERVICE_NAME` | unset, `enterprise-rag` | OTLP endpoint for traces (needs `opentelemetry-sdk` and the OTLP exporter installed). |

### Production settings

Set `ERAG_ENV=prod`, strong `ERAG_SECRET_KEY` and `ERAG_MCP_CONTEXT_SECRET`, a master key (`ERAG_SECRETS_KEY` or `_FILE`), a strong `ERAG_RUNNER_TOKEN` if the runner is used, `ERAG_SEED_DEMO=false`, and `ERAG_COOKIE_SECURE=true` behind HTTPS. The app refuses to start in prod with development secrets or without a master key. Enable MFA for administrators. See [`deploy/.env.example`](deploy/.env.example).

## Security model and known boundaries

What the backend guarantees: every chunk and tool result a model sees was authorized for the asking user at that moment; confidential data is only processed by local models; tools are offered per role and called through one gateway with schema, constraint, clearance and rate checks; secrets are encrypted at rest, never returned by the API and never given to the model; every administrative action and every tool, model and secret use is audited. Details: [`docs/threat-model.md`](docs/threat-model.md), [`docs/architecture.md`](docs/architecture.md), [`docs/adr/`](docs/adr/README.md).

Known boundaries, documented rather than hidden:

- **Source permissions are not mirrored.** Connector content inherits the target container's access; a source whose audience is narrower than the container's leaks to every reader of that container.
- **Third-party MCP output cannot be filtered per user.** We do not know the permissions of the system behind it. It is controlled per tool and role, labeled (default `confidential`) and kept on cleared models; arguments are limited by the server's clearance.
- **The master key sits on the same host as the app.** The credential store protects against database and backup leaks, not against compromise of that host.
- **Admin account compromise remains the largest risk.** Mitigations: optional MFA, step-up for sensitive actions, audit, the runner's confinement. The audit log lives in the same database (no tamper evidence).
- **Container isolation is not a VM**; runner MCPs get network `none` only (allowlisted egress is not implemented).
- **Admin-added cloud providers are trusted with what their clearance allows.** The software does not verify their retention or training policies, and a public endpoint can never be declared local.
- **A provider whose price is unknown** and that does not report cost is bounded by requests and tokens, not money. (Budgets themselves are now reserved: concurrent requests cannot overshoot them.)
- **Rate-limit counters are not durable.** They live in an UNLOGGED table: a database crash resets them to zero. Money (budget reservations, usage) is always logged.
- **Permission changes made with raw SQL** outside the application do not invalidate the permission cache; such scripts must increase `authz_state.epoch` themselves (or restart the app).
- **The model can still be wrong** or repeat what it was legitimately given.

## Evaluation

Retrieval changes are measured before they are made (ADR 0040). A question set is asked **as personas**, through the same retrieval path as the chat:

```bash
uv run erag eval prepare        # create, migrate and load the evaluation database (erag_eval), never the app's
uv run erag eval run            # ask every question as every persona; writes eval/runs/<time>-acme.json
uv run erag eval compare A B    # metric, per-tag and per-question differences between two reports
uv run erag eval gate           # fails on any leak, or a drop of more than 0.02 in recall@8 or MRR
uv run erag eval baseline       # store eval/baselines/acme.json (a deliberate commit)
```

- **The set** (`eval/sets/acme.yaml`, about 150 questions and 250 runs) covers the ACME demo plus a generated evaluation corpus (`eval/corpus/acme-eval/`, about 250 documents: Spanish contracts with near-duplicates, support articles in two languages, product sheets with near-identical specifications, invoices, restricted HR files). Every item says, per persona, which documents should come back, whether it is answerable, and what the persona must never see:

  ```yaml
  - id: hw-040
    question: What is the salary band for a senior engineer?
    as_users: [hr@acme.test, engineer@acme.test]
    expect:
      hr@acme.test: { documents: [hr-confidential/salary-bands-2026.md] }
      engineer@acme.test: { answerable: false, must_not_see: [hr-confidential/salary-bands-2026.md] }
    tags: [forbidden, en]
  ```
- **Leakage** is checked twice: against `must_not_see`, and against an oracle built from the declared structure (not from the permission code). Any leak fails the run.
- **Metrics:** recall@5/8, MRR and nDCG@10 at document level (a document's rank is its first chunk's rank, so recall@8 means "in the chat's context"), abstention on unanswerable questions, per-tag figures and latency.
- **No Ollama in CI:** `eval/cache/bge-m3.bin` holds the real `bge-m3` embeddings of the corpus and the questions, keyed by text hash. Live and recorded runs give identical numbers. After changing the corpus, the chunker or the questions, re-record them on a machine with Ollama: `uv run erag eval embed-cache` (or `erag eval prepare --live`), and commit the file. `uv run erag eval generate` rewrites the generated corpus and questions.
- **Baseline** (vector search only, R13): recall@8 0.980, MRR 0.899, nDCG@10 0.921, abstention 0.307, no leaks. The weak spots were exact identifiers in Spanish contracts and abstention.
- **Hybrid search without the reranker** (the fallback, measured 2026-11-18): recall@8 1.000, MRR 0.983, nDCG@10 0.988, abstention 0.287, no leaks; the gate passes. `halfvec` storage gives exactly the baseline figures ([`docs/benchmarks/r18-halfvec.md`](docs/benchmarks/r18-halfvec.md)).
- **Override a retrieval setting for one run:** `erag eval run --retrieval mode=vector --retrieval reranker=`.
- **Feedback:** thumbs up or down on every answer; a thumbs down can be proposed as an evaluation item, reviewed in Admin > Evaluation by an administrator who can read every cited source, and run against the live database as the company's own set.
- **At scale:** `scripts/synth_corpus.py --chunks N` loads synthetic vectors into `erag_bench`, and `scripts/bench_search.py --out docs/benchmarks/<name>.md` measures filtered search latency and recall for users who see 90, 10 and 1 percent of the corpus.

## Development

```bash
uv sync                                                  # Python 3.12 deps
docker compose -f deploy/docker-compose.yml up -d postgres
uv run alembic upgrade head
uv run pytest                                            # needs Postgres; Ollama- and Docker-backed tests skip without them
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run python scripts/check_licenses.py
uv run erag eval prepare && uv run erag eval gate           # retrieval quality and leakage gate (no Ollama needed)
cd frontend && npm ci && npm run build
```

Useful commands: `uv run erag create-admin --email you@example.com`, `uv run erag seed-acme`, `uv run erag worker`, `uv run erag secrets init|set|list|rotate-master`, `uv run erag reset-mfa --email ...`, `uv run python -m app.runner.service`, `uv run erag eval prepare|run|compare|gate|baseline|embed-cache|generate`, `uv run erag models list|install <name>`, `uv run erag reindex [--container slug] [--all]`, `uv run erag maintenance`, `uv run erag blobs migrate --to s3`, `uv run erag backup verify [--full]`, `uv run python scripts/restore_drill.py --help`. Architecture decisions are in [`docs/adr/`](docs/adr/README.md).

Tests against real external servers run when their address is given: `ERAG_TEST_S3_ENDPOINT` (SeaweedFS, see `tests/integration/test_s3_seaweedfs.py`) and `ERAG_TEST_PGBOUNCER_URL` (`tests/integration/test_pgbouncer.py`).

## License

EnterpriseRAG is **source-available**, not open source.

- **Software:** [PolyForm Noncommercial License 1.0.0](LICENSE). You can read, run, modify and share it for noncommercial purposes (personal use, research, study, hobby projects, and use by educational, charitable, public-research and government institutions).
- **Commercial use** (including running it inside a company for business purposes) requires a commercial license from the licensor, Alvaro Badia (Badia Partners). See [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md).
- **White paper:** [CC BY 4.0](docs/whitepaper/LICENSE-CC-BY-4.0.txt), see [`docs/whitepaper/`](docs/whitepaper/).

### 60-day evaluation permit

In addition to the license above, the licensor (Alvaro Badia, under the Badia Partners brand) grants any company or organization permission to install and run EnterpriseRAG for **evaluation only**, for up to **60 days** from first installation, under these conditions:

- Evaluation means testing whether the software fits your needs, in a non-production environment. No production use and no use to operate your business.
- Do not redistribute it or offer it to third parties.
- After 60 days, stop using it or obtain a commercial license.
- You do not need to ask or notify us. This permit is provided as-is and may be withdrawn for future versions.

## Consulting and implementation

Need help adopting this in your company? Implementation and consulting are offered by Badia Partners: https://badiapartners.com

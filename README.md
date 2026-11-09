# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://badiapartners.com).

> **Status: v0.3 in development** (branch `feat/v0.3`, on top of v0.2). Runnable end to end with a seeded demo company. Progress and verification notes: [`PROGRESS.md`](PROGRESS.md). Plans: [`docs/PLAN.md`](docs/PLAN.md) (MVP), [`docs/PLAN-v0.2.md`](docs/PLAN-v0.2.md) (administration, connectors, MCP extensibility), [`docs/PLAN-v0.3.md`](docs/PLAN-v0.3.md) (cost limits and quotas, paid models, model router).

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
- **MFA and step-up:** optional TOTP for every user with recovery codes; re-authentication within 5 minutes for sensitive admin actions (secrets, providers and the default model, connectors, MCP registration, admin promotion, MFA resets).
- **Connectors:** `folder` (below allowed roots, no symlink escapes) and `web` (URLs or a sitemap, SSRF-guarded) sync into one container and inherit its permissions; scheduled or manual, with dry-run preview and run history.
- **Ingestion:** PDF, DOCX, XLSX, HTML, Markdown and text; SHA-256 dedupe, versioning, a Postgres-backed job queue, local `bge-m3` embeddings.
- **Authorized retrieval:** the user's permissions are resolved from PostgreSQL on every request and pushed into the pgvector query as a filter, then re-checked. Revocation is immediate.
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
- **Budgets are checked, not reserved.** Concurrent requests can each pass the worst-case check and overshoot a budget or a pool by at most their own worst cases. A provider whose price is unknown and that does not report cost is bounded by requests and tokens, not money.
- **The model can still be wrong** or repeat what it was legitimately given.

## Development

```bash
uv sync                                                  # Python 3.12 deps
docker compose -f deploy/docker-compose.yml up -d postgres
uv run alembic upgrade head
uv run pytest                                            # needs Postgres; Ollama- and Docker-backed tests skip without them
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run python scripts/check_licenses.py
cd frontend && npm ci && npm run build
```

Useful commands: `uv run erag create-admin --email you@example.com`, `uv run erag seed-acme`, `uv run erag worker`, `uv run erag secrets init|set|list|rotate-master`, `uv run erag reset-mfa --email ...`, `uv run python -m app.runner.service`. Architecture decisions are in [`docs/adr/`](docs/adr/README.md).

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

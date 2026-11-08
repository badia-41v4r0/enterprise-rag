# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://badiapartners.com).

> **Status: v0.2 in development** (branch `feat/admin-extensibility`, on top of the `v0.1.0` MVP). Runnable end to end with a seeded demo company. Progress and verification notes: [`PROGRESS.md`](PROGRESS.md). Plans: [`docs/PLAN.md`](docs/PLAN.md) (MVP), [`docs/PLAN-v0.2.md`](docs/PLAN-v0.2.md) (administration, connectors, MCP extensibility).

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
- **Credential store:** connector, MCP and OpenRouter secrets encrypted at rest (AES-256-GCM, master key outside the database), write-only, every use audited.
- **MFA and step-up:** optional TOTP for every user with recovery codes; re-authentication within 5 minutes for sensitive admin actions (secrets, providers, connectors, MCP registration, admin promotion, MFA resets).
- **Connectors:** `folder` (below allowed roots, no symlink escapes) and `web` (URLs or a sitemap, SSRF-guarded) sync into one container and inherit its permissions; scheduled or manual, with dry-run preview and run history.
- **Ingestion:** PDF, DOCX, XLSX, HTML, Markdown and text; SHA-256 dedupe, versioning, a Postgres-backed job queue, local `bge-m3` embeddings.
- **Authorized retrieval:** the user's permissions are resolved from PostgreSQL on every request and pushed into the pgvector query as a filter, then re-checked. Revocation is immediate.
- **Classification routing:** after retrieval (and after every tool result) the effective classification decides which models may answer. Confidential data only ever goes to local models (Ollama). OpenRouter is optional, free models only, with a hard monthly ceiling.
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

### Optional: OpenRouter (free models only)

Set the key in **Admin > Providers**: it is stored encrypted in the credential store and can be changed without a restart. `ERAG_OPENROUTER_API_KEY` (in `deploy/.env` for Docker Compose, or `.env` at the repo root) is only a bootstrap fallback used while the store has no key; to move an existing key into the store run `erag secrets set openrouter_api_key --from-env ERAG_OPENROUTER_API_KEY` and then remove the variable. Only models whose id ends in `:free` are accepted, and a hard monthly spend ceiling (`ERAG_OPENROUTER_MONTHLY_BUDGET_USD`, default `0.01`; the UI can only lower it) is enforced before every request ([ADR 0016](docs/adr/0016-openrouter-free-only-and-spend-ceiling.md), [ADR 0018](docs/adr/0018-credential-store.md)). Confidential data never goes to the cloud; what else may go is controlled by `ERAG_CLOUD_MAX_CLASSIFICATION`.

## Configuration reference (new in v0.2)

All settings use the `ERAG_` prefix (full list in [`backend/app/core/config.py`](backend/app/core/config.py)).

| Setting | Default | Purpose |
|---|---|---|
| `ERAG_SECRETS_KEY` / `ERAG_SECRETS_KEY_FILE` | dev: derived from `ERAG_SECRET_KEY`; Compose: `/data/secrets.key` (generated on first start) | Credential store master key (32 random bytes, base64). Create with `erag secrets init [--file F]`. **Required in prod.** Back it up outside the database: losing it loses all secrets and MFA enrolments. Rotate with `erag secrets rotate-master --new-key-file F`. |
| `ERAG_STEP_UP_SECONDS` | `300` | How recent a re-authentication must be for sensitive admin actions. |
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

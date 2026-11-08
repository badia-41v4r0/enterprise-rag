# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://badiapartners.com).

> **Status: MVP in development (v0.1.0.dev0).** Runnable end to end with a seeded demo company. Progress and verification notes: [`PROGRESS.md`](PROGRESS.md). Plan: [`docs/PLAN.md`](docs/PLAN.md).

## The idea

Most enterprise RAG and agent setups let the model see (or reach) more than the person asking is allowed to see. EnterpriseRAG is built around a few principles:

1. The model never decides permissions; the backend does.
2. Access control is applied during retrieval, before the LLM sees anything.
3. Every tool call goes through a gateway with authorization and audit, and tools only receive data the user is already allowed to see.
4. Data classification decides which models (local or cloud) may process it.
5. Secrets never reach the browser or the model.

## What is in the MVP

- **Identity and permissions:** server-side sessions (HttpOnly cookie + CSRF), roles, containers classified `public / internal / confidential`, allow-only grants, restricted documents, per-tool `execute` grants, audit log. One admin manages everything from the UI.
- **Ingestion:** PDF, DOCX, XLSX, HTML, Markdown and text; SHA-256 dedupe, versioning, a Postgres-backed job queue, local `bge-m3` embeddings.
- **Authorized retrieval:** the user's permissions are resolved from PostgreSQL on every request and pushed into the pgvector query as a filter, then re-checked. Revocation is immediate.
- **Classification routing:** after retrieval (and after every tool result) the effective classification decides which models may answer. Confidential data only ever goes to local models (Ollama).
- **MCP:** a gateway (MCP client) with tool authorization, a signed short-lived permission context for the servers, a result validator, and two reference servers (`files`, `tickets`).
- **Frontend:** React chat with streaming answers and clickable citations, plus an admin panel.
- **ACME Corp demo** and a security test suite (canary leakage matrix, revocation, routing, tool and injection tests). See [`demo/acme/README.md`](demo/acme/README.md).

## Quick start (demo)

Requirements: Docker with Compose v2, and [Ollama](https://ollama.com) with two models:

```bash
ollama pull bge-m3      # embeddings (1.2 GB)
ollama pull qwen3:4b    # local chat model (2.5 GB)
```

Then, from the repo root:

```bash
# Ollama on the host listening on 127.0.0.1 (default install): share the host network
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build

# or, if your Ollama listens on 0.0.0.0 (OLLAMA_HOST=0.0.0.0):
docker compose up -d --build
```

Open http://127.0.0.1:8000 and log in as `hr@acme.test`, `engineer@acme.test`, `finance@acme.test`, `newhire@acme.test`, `exec@acme.test` or `admin@acme.test` (password `acme-demo-password`). The demo is seeded on start-up; ingestion of the 12 documents takes a few seconds. Then run the scripted scenarios:

```bash
uv run python scripts/demo_scenarios.py --base http://127.0.0.1:8000
```

Set `APP_PORT` if port 8000 is taken. A bundled Ollama container exists behind `--profile ollama` (NVIDIA GPU via CDI; not verified, see [ADR 0006](docs/adr/0006-ollama-on-host.md)). The demo secrets and password are for local use only: set `ERAG_SECRET_KEY`, `ERAG_MCP_CONTEXT_SECRET`, `ERAG_ENV=prod` and `ERAG_SEED_DEMO=false` for anything else (see [`deploy/.env.example`](deploy/.env.example)).

## Development

```bash
uv sync                                                  # Python 3.12 deps
docker compose -f deploy/docker-compose.yml up -d postgres
uv run alembic upgrade head
uv run pytest                                            # needs Postgres; Ollama-backed tests skip without it
uv run ruff check . && uv run ruff format --check . && uv run mypy
cd frontend && npm ci && npm run build
```

Useful commands: `uv run erag create-admin --email you@example.com`, `uv run erag seed-acme`, `uv run erag worker`. Architecture decisions are in [`docs/adr/`](docs/adr/README.md).

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

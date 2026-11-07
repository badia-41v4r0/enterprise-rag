# EnterpriseRAG MVP: progress report

Branch: `feat/mvp` (not pushed). Plan: [`docs/PLAN.md`](docs/PLAN.md). Decisions: [`docs/adr/`](docs/adr/README.md).

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0. Foundations | Done | Compose, Alembic, config, CI workflow, ADRs |
| 1. Identity and permissions | Not started | |
| 2. Ingestion | Not started | |
| 3. Authorized retrieval and chat | Not started | |
| 4. Frontend | Not started | |
| 5. MCP | Not started | |
| 6. ACME demo and hardening | Not started | |
| 7. Docs, release | Not started | |

## Verified (Phase 0)

- `uv run alembic upgrade head` / `downgrade base` / `upgrade head` round-trip on PostgreSQL 16 + pgvector.
- `APP_PORT=8088 docker compose up -d --build app` then `curl localhost:8088/health` returned `{"status":"ok","db":true}`.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest` pass locally.
- `uv run python scripts/check_licenses.py`: no copyleft dependencies.
- The GitHub Actions workflow (`.github/workflows/ci.yml`) was written but **not run** (nothing is pushed).

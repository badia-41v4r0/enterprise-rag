# 0001. Record architecture decisions; stack from PLAN.md

**Status:** Accepted (2026-11-08)

## Context
The MVP is built largely autonomously against `docs/PLAN.md`. Ambiguities must be resolved without blocking, and the owner must be able to review every non-obvious choice.

## Decision
- Every non-obvious decision gets a short ADR in `docs/adr/`.
- The stack is the one fixed in PLAN.md section 3: Python 3.12, FastAPI, Pydantic v2, `uv`, `ruff`, `mypy`, `pytest`; PostgreSQL 16 + pgvector with SQLAlchemy 2 and Alembic; jobs in an `ingestion_jobs` table consumed with `FOR UPDATE SKIP LOCKED` (no Redis); React + TypeScript + Vite frontend served by FastAPI from the same origin; Ollama (local) and OpenRouter (cloud) behind one OpenAI-compatible client; official `mcp` Python SDK.
- The design deltas of PLAN.md section 2 are recorded individually in ADRs 0002, 0003, 0005 and 0010.

## Consequences
ADRs double as raw material for the white paper (sections 3 to 5).

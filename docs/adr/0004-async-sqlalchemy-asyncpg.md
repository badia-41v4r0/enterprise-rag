# 0004. Async SQLAlchemy with asyncpg (no psycopg)

**Status:** Accepted (2026-11-08)

## Context
psycopg (2 and 3) is LGPL-3.0. PLAN.md section 14 allows only permissive dependencies. The MCP SDK and FastAPI are async-first.

## Decision
Use SQLAlchemy 2 in async mode with the `asyncpg` driver (Apache-2.0), also for Alembic migrations. A `scripts/check_licenses.py` CI step fails on any GPL/AGPL/LGPL dependency.

## Consequences
All DB code is async. The worker uses `SELECT ... FOR UPDATE SKIP LOCKED` through the same driver.

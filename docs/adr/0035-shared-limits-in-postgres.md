# 0035. Shared limits in Postgres behind `LimitStore`

**Status:** Accepted (2026-11-18, PLAN-v0.5 decision 3).

## Context
Rate limiters (login, MFA, chat, tools) were in-process sliding windows: with two replicas every limit doubled, and an attacker could spread attempts across replicas. The project avoids Redis, and Postgres is already a required, backed-up dependency.

## Decision
- A `LimitStore` interface with one Postgres implementation: an **UNLOGGED** `rate_limit_counters` table, one row per key and fixed window. A check is a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` (with the previous window read in the same statement) on an **autocommit** connection, so it takes no lock inside the request's transaction.
- The window is the usual sliding-window approximation: current window plus the previous one weighted by its remaining overlap. Refused attempts count too, so hammering keeps the door closed.
- UNLOGGED: not WAL-logged, faster, emptied after a crash. Losing rate-limit counts at a crash is acceptable; money never lives there (ADR 0036).
- Old windows are deleted by the maintenance task. A memory store exists for unit tests only.

## Consequences
- Limits hold across any number of replicas.
- One small write per limited request. Postgres handles thousands per second; a Valkey `LimitStore` stays in the ROADMAP for installations that outgrow it.
- After a database crash, counters restart from zero.

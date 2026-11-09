# 0040. `ERAG_SEED_DEMO` with the setup wizard

**Status:** Accepted (2026-11-09), owner cleanup request for PLAN-v0.4

## Context
Until v0.3 the Compose default was `ERAG_SEED_DEMO=true`: every `docker compose up` seeded ACME Corp including
`admin@acme.test` with the published demo password (ADR 0015). With the wizard, a fresh installation must come up in
setup mode with no demo data unless the administrator chooses the demo, while development and
`scripts/demo_scenarios.py` still need a one-command seeded demo.

## Decision
- The Compose default is now **`ERAG_SEED_DEMO=false`**: a fresh installation starts in setup mode.
- **`ERAG_SEED_DEMO=true`** keeps the old behaviour for development: `erag bootstrap` seeds the full demo, including
  `admin@acme.test`, and records the setup as completed (`completed_via = "seed_demo"`), so the wizard is skipped.
  `scripts/demo_scenarios.py` and `scripts/ui_smoke.mjs` run against such an installation.
- It is **ignored in production** (`ERAG_ENV=prod`) with a log line: a production setup never gets an administrator
  with a published password (the wizard's demo option creates the personas without `admin@acme.test`, ADR 0036).
- An installation that was seeded before (for example the owner's running demo) keeps its data: migration 0012
  records its setup as completed (ADR 0034), and with the new default nothing is re-seeded at start-up (the seed was
  idempotent anyway).
- `ERAG_OPENROUTER_ALLOW_PAID` (removed in v0.3, ADR 0028) is no longer passed through by the Compose file. The
  application still warns if it is set in the environment.

## Consequences
- Supersedes the "compose default" part of ADR 0015.
- To get the old demo with one command: `ERAG_SEED_DEMO=true docker compose ... up -d --build`.

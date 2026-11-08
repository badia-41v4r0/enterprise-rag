# 0015. One-command seeded demo and MVP hardening

**Status:** Accepted (2026-11-08) — **Owner review**

## Decision
- The `app` container runs `alembic upgrade head`, then `erag bootstrap`, then uvicorn. `bootstrap` seeds ACME Corp when `ERAG_SEED_DEMO=true`, which is the **compose default** so that `docker compose up` gives a working demo. Seeding is idempotent and queues documents for the worker (blobs live on the shared volume, so seeding must run inside the containers, not from the host).
- Demo password `acme-demo-password` (override with `ERAG_DEMO_PASSWORD`). Dev secrets are accepted only when `ERAG_ENV` is not `prod`.
- Hardening in the MVP: security headers (strict CSP, `frame-ancestors 'none'`, nosniff, no-referrer), `Cache-Control: no-store` on the API, request IDs and JSON request logs, rate limits for login (per IP+email), chat (per user) and tools (per user+tool), upload size limit.
- Not done: OpenRouter monthly spend ceiling (no key to test against), tamper-evident audit, distributed rate limiting.

## Owner review
For a public demo server, set `ERAG_SEED_DEMO=false` after the first start or change the demo password; the personas are well known.

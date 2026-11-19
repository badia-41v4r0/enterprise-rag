# 0041. Horizontal scaling: stateless replicas, PgBouncer and advisory locks

**Status:** Accepted (2026-11-18).

## Decision
- **Nothing that must be shared lives in a process.** Sessions were already in Postgres; v0.5 moves rate limits (ADR 0035), budgets (ADR 0036), the generation queue (ADR 0037) and permission invalidation (ADR 0034) there too. In-process state left is a cache that is either keyed by the epoch or harmless to lose (planner estimates, the router build).
- **Singletons by advisory lock.** The connector scheduler and the maintenance task (expired and idle sessions, conversation retention, expired reservations and leases, old rate-limit windows) take `pg_try_advisory_xact_lock`; the process that gets it works, the others skip that round. No leader election service.
- **Workers scale out** on the existing `FOR UPDATE SKIP LOCKED` queue, now with priorities, backoff, dead letters, a heartbeat and a reaper (R18).
- **Two connection pools per process** (found by the R20 load test): requests use one; short side operations that run while a request holds a connection (rate limits, session activity, reservations, queue leases) use their own (`ERAG_DB_SIDE_POOL_SIZE`), so a request never waits for a second connection from its own pool. A chat gives its connection back while it waits in the queue and while the model streams.
- **PgBouncer in transaction mode** in front of Postgres: `ERAG_DB_PGBOUNCER=true` turns off asyncpg's statement cache and gives every prepared statement a unique name; nothing relies on session state (retrieval uses `SET LOCAL` only); `LISTEN` uses `ERAG_DB_DIRECT_URL`. Tested against a real PgBouncer 1.24 (20 concurrent sessions over 5 server connections).
- **Graceful shutdown.** uvicorn stops accepting on SIGTERM and lets chat streams finish (60 s); a worker finishes the jobs it holds (120 s grace in Compose).
- **Compose `scale` profile**: PgBouncer, a second app and a second worker. Kubernetes: the Helm chart (ADR 0045).

## Consequences
- Throughput grows with replicas until Postgres or the model servers saturate: in the R20 load test a second replica took chat throughput from 9.6 to 18.1 per second when the app was the bottleneck, while the model-slot limit held exactly across replicas (`docs/benchmarks/r20-load.md`).
- Every replica needs a direct connection for `LISTEN` besides its pooled ones.

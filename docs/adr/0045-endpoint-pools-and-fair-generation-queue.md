# 0045. Endpoint pools and the fair generation queue

**Status:** Accepted (2026-11-18).

## Context
A local model server handles a few generations at once per GPU. With many users, requests above that either overload it (everyone slows down) or fail. One user running a script could take every slot.

## Decision
- **Endpoint pools.** A provider has its primary URL plus extra `llm_endpoints` (one Ollama or vLLM per GPU, for example), each with `max_concurrency`. Every endpoint of a pool must have the provider's locality: checked when added (a step-up action, audited) and again at every router build; a mismatched endpoint is left out. A provider only ever sends to its own endpoints, and the egress guard (ADR 0025) still checks each request's destination.
- **Handoff.** A release, under the pool lock, gives the slot directly to the next waiter in fair order (a lease with the waiter's id) and notifies that waiter only; waiters look up their own lease by primary key without the lock, and every few seconds look for slots freed without a release (expired leases). The R20 load test showed why: with every waiter competing for the lock on each release, dispatch fell to a quarter of capacity.
- **Leases.** A request needs a lease on one endpoint before it streams: a row in `generation_leases` with an expiry, extended by a heartbeat while streaming, so a dead replica gives its slots back within a minute. The least-loaded endpoint is chosen.
- **Fair waiting.** When the pool is full the request waits in `generation_waiters`. A free slot goes to the highest **role priority**, then to the **user with the fewest generations in flight**, then to the **oldest** waiter. Decisions are made under a per-pool advisory lock, so replicas never over-allocate.
- **Wake-ups.** A grant sends `NOTIFY` with the waiter's id; each process holds one direct `LISTEN` connection (not through PgBouncer) and also re-checks every second, so a lost notification costs latency only.
- The chat shows "waiting, position N"; after `ERAG_GENERATION_QUEUE_TIMEOUT_SECONDS` the request ends with a clear message and nothing was sent.
- **Unlimited pools skip all of this** (no `max_concurrency` anywhere): no database round-trip for the common single-GPU setup.
- **Query embeddings first.** Chat questions can use a dedicated embedding endpoint (`ERAG_QUERY_EMBED_URL`); bulk ingestion spreads over its own pool (`ERAG_INGEST_EMBED_URLS`).

## Consequences
- Capacity grows by adding endpoints; fairness holds across replicas (tests cover in-flight fairness, priority, timeouts, dead leases, least-loaded choice).
- Two to three small writes per limited request; the queue depth and wait time are metrics with alerts.

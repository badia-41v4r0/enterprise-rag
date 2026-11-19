# Load test: replicas, the generation queue and shared limits (R20, measured 2026-11-19)

**What is measured:** EnterpriseRAG itself under many concurrent users: login, sessions, retrieval (hybrid, without the reranker), budget reservations, the fair generation queue and usage records, with one and two app replicas on the same Postgres. The model server is the stand-in `loadtest/fake_model_server.py` (deterministic embeddings; a streaming answer of 25 tokens × 40 ms ≈ 1 s), because the question is whether **our** code scales and keeps its limits, not how fast a GPU is.

**Setup:** `loadtest/README.md`. 300 users imported through the app's CSV import (each simulated user logs in as a distinct one); Locust, 200 users, spawn 20/s, 3 minutes, think time 2-6 s, mostly chat (6 : 2 : 1 with listing containers and own usage). Default rate limits (login 10/min per address and email, chat 20/min per user). Everything on one machine: AMD Ryzen 3 3100 (4 cores, 8 threads), 16 GB RAM; Postgres 16 in Docker, the replicas (`uvicorn`, one process each), Locust and the fake server share the CPU.

| Run | Replicas | Model slots (`max_concurrency`) | Chats/s | First token p50 / p95 | Other requests p50 | Peak concurrent generations | Failures |
|---|---|---|---|---|---|---|---|
| A1 | 1 | 8 | 5.2 | 26.0 / 27.0 s | 85-110 ms | **8** | 0.07% |
| A2 | 2 | 8 | 5.5 | 20.0 / 50 s | 16-21 ms | **8** | 0.07% |
| B1 | 1 | 64 | 9.6 | 5.9 / 43 s | 130-170 ms | 35 | 0.11% |
| B2 | 2 | 64 | **18.1** | **1.9** / 7.2 s | 36-54 ms | 60 | 0.04% |

## Reading

- **Limits hold across replicas.** With 8 model slots the stand-in server never saw more than 8 generations at once, with one replica or two (A1, A2). With 64 slots it never saw more than 64.
- **When the model is the bottleneck** (A), more replicas cannot raise chat throughput: 8 slots of about 1 s allow at most 8 chats/s, and 5.2-5.5 were measured (the rest is per-generation overhead: reservation, lease handoff, usage record, on a shared CPU); requests wait in the fair queue (the first-token time is the queue wait). The other endpoints get faster with a second replica.
- **When the app is the bottleneck** (B), a second replica nearly doubles chat throughput (×1.89) and cuts the median time to the first token from 5.9 s to 1.9 s, on a machine whose 8 threads also run Postgres, Locust and the model stand-in. Throughput grows roughly in proportion, as PLAN-v0.5 section 2 requires.
- **Failures** (under 0.12%) are `HTTP 0` streams cut when Locust stops at the end of the run, and a few connection resets under peak load; no 5xx.

## Found and fixed by this test

1. **Connection pool exhaustion (500s at login and on every endpoint).** Rate limits, session activity, reservations and queue leases took a second connection from the same pool while the request held one; under load every request waited for a second connection and the pool ran dry (`QueuePool limit ... reached`). Now these short operations use their own pool (`ERAG_DB_SIDE_POOL_SIZE`), and a chat gives its connection back while it waits in the queue and while the model streams, so connections scale with database work, not with open conversations.
2. **Queue dispatch limited to a quarter of capacity.** On every release, every waiter woke and competed for the pool lock, each running several queries: with 150 waiters, 8 slots of ~1 s produced 2.1 chats/s. Now the releasing request hands the slot directly to the next waiter in fair order (a lease with the waiter's id) and notifies only that waiter; waiters check their own lease by primary key without the lock. Same scenario: 5.2 chats/s, the slot limit still exact.
3. **The load setup** limited a provider row that did not exist yet (rows are created at app start); fixed.

## Not measured here

1,000 simulated users (this machine runs out of CPU well before: Locust itself, two replicas, Postgres and the stand-in share 8 threads), real GPUs, the reranker under load (about 1.3 s of CPU per search here: it needs a GPU or its own capacity planning), and ingestion throughput.

# Scaling and capacity

## What scales how

| Component | Scales by | Shared state |
|---|---|---|
| App replicas (API, chat streams, reranker) | more replicas behind the proxy | Postgres: sessions, permission epoch, rate limits, budget reservations, generation queue (ADR 0049) |
| Workers (ingestion, connector syncs) | more workers, `ERAG_WORKER_CONCURRENCY` jobs each | the job queue (`SKIP LOCKED`), heartbeats, a reaper |
| Chat models | more endpoints per provider (Admin > Providers > endpoints), `max_concurrency` each | leases and the fair queue (ADR 0045) |
| Query embeddings | a dedicated endpoint: `ERAG_QUERY_EMBED_URL` | |
| Ingestion embeddings | a pool: `ERAG_INGEST_EMBED_URLS` (comma-separated), `ERAG_INGEST_EMBED_CONCURRENCY` in flight per worker | |
| Postgres connections | PgBouncer in transaction mode (`ERAG_DB_PGBOUNCER=true`, `ERAG_DB_DIRECT_URL` for LISTEN) | |
| Blobs | S3-compatible storage (`ERAG_BLOB_BACKEND=s3`) | |

Compose: `docker compose --profile scale up -d` adds PgBouncer, a second app and a second worker.

## Sizing the model servers

The generation queue makes overload visible instead of slow for everyone: set `max_concurrency` per endpoint to what it serves at an acceptable speed, then add endpoints until the queue wait (`erag_stage_seconds{stage="queue_wait"}`) stays low.

Rule of thumb for concurrent generations per GPU (vLLM or Ollama with `OLLAMA_NUM_PARALLEL`), 4-8 B instruct models at 4-bit:

| GPU memory | Concurrent generations at a usable speed |
|---|---|
| 8 GB | 2-4 |
| 16 GB | 4-8 |
| 24 GB | 8-16 |
| 48-80 GB | 16-32 (vLLM recommended) |

These are starting points, not measurements of this project; measure with your model and context length (the load test in R20).

**From users to slots.** Of the people who can use the assistant, typically 10-20% are active in a busy hour, and each active person has a generation running a small fraction of the time (an answer takes 5-20 s; people read and type for minutes). For 10,000 users: about 1,500 active in the peak hour, and with one question every 3 minutes and 10 s per answer, about 1,500 / 180 × 10 ≈ **85 concurrent generations** at peak. That is roughly 6-10 GPUs of 24 GB, or fewer with a larger model server. Watch `erag_generation_queue_depth` and `erag_generation_queue_timeouts_total` and adjust.

## Ingestion throughput

Target: 50,000 documents per day on one GPU host (PLAN-v0.5 5.2). Embedding is the bottleneck: run the embedding pool on the GPU, raise `ERAG_INGEST_EMBED_CONCURRENCY`, add workers. Connectors pause enqueueing above `ERAG_INGEST_QUEUE_MAX` (default 5,000 queued jobs). Files above `ERAG_INGEST_MAX_BLOB_MB` (default 200) are refused before they are read into memory.

## Postgres

- Reference size: 20 million chunks of 1,024 dimensions. As `halfvec` that is about 41 GB of vectors plus the HNSW index; give Postgres memory for the index (shared buffers + page cache) and set `maintenance_work_mem` high (several GB) with `max_parallel_maintenance_workers` for index builds.
- Selective users (who read under 20,000 chunks and 10% of the corpus) are served by an exact scan of their containers; others by the HNSW index with iterative scan (ADR 0042).
- Pool per replica: `ERAG_DB_POOL_SIZE` (10) + `ERAG_DB_MAX_OVERFLOW` (10); behind PgBouncer, size PgBouncer's `DEFAULT_POOL_SIZE` to what Postgres handles (a few per CPU core).

## Upgrades without downtime

Migrations follow **expand/contract**: a release never drops or renames something the previous release still reads; a later release removes it. A test (`tests/unit/test_migrations_expand_contract.py`) rejects migrations that drop or rename columns or tables in `upgrade()` unless marked as a contract step. Roll the app replicas one at a time behind a readiness gate (`/health/ready`).

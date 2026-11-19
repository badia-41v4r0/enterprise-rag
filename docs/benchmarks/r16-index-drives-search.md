# Vector search benchmark

Produced by `scripts/synth_corpus.py` + `scripts/bench_search.py`.

- Chunks: 60,000 in 500 containers (synthetic, clustered unit vectors, dim 1024)
- Hardware: AMD Ryzen 3 3100 4-Core Processor, 8 threads, 16 GB RAM
- PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2), pgvector 0.8.7, HNSW m=16 ef_construction=64 (defaults), ef_search=100, iterative_scan=strict_order
- 50 queries per row; recall against an exact scan on the first 20
- Measured 2026-11-18

| Readable | k | p50 ms | p95 ms | fewer than k | recall vs exact |
|---|---|---|---|---|---|
| 90% | 8 | 16.0 | 174.0 | 0/50 | 0.938 |
| 90% | 50 | 19.7 | 25.8 | 0/50 | 0.942 |
| 10% | 8 | 30.1 | 33.6 | 0/50 | 1.000 |
| 10% | 50 | 38.4 | 41.2 | 0/50 | 1.000 |
| 1% | 8 | 7.7 | 9.3 | 0/50 | 1.000 |
| 1% | 50 | 16.4 | 19.0 | 0/50 | 1.000 |

## What changed since r13-baseline.md

- `PgVectorStore.search` now runs an inner query on `chunks` alone (`container_id IN readable`, ordered by distance, limited) and joins documents afterwards (current version, restricted documents). Candidates rejected by the document filter are compensated by fetching four times more, up to 2,000.
- With broad access the planner now uses the HNSW index: the 90 percent user drops from 1,046 ms to about 15 ms at k = 8. With narrow access it still picks the container index and an exact sort, which is fast because few rows qualify. That is the "plan for selective users" of PLAN-v0.5 3.1, chosen by the planner from real statistics instead of a hand-made threshold.
- Recall against an exact scan is about 0.92 for broad access, the price of an approximate index at `ef_search = 100` on synthetic vectors (real embeddings cluster more and score higher). HNSW tuning is R18.
- Correction to r13-baseline.md: its query vectors carried far too much noise (0.3 per dimension, not 0.3 in total). Its latencies are valid, because every row was an exact scan, but its recall column is not meaningful.

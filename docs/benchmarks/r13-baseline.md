# Vector search benchmark

Produced by `scripts/synth_corpus.py` + `scripts/bench_search.py`.

- Chunks: 60,000 in 500 containers (synthetic, clustered unit vectors, dim 1024)
- Hardware: AMD Ryzen 3 3100 4-Core Processor, 8 threads, 16 GB RAM
- PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2), pgvector 0.8.7, HNSW m=16 ef_construction=64 (defaults), ef_search=100, iterative_scan=strict_order
- 50 queries per row; recall against an exact scan on the first 20
- Measured 2026-11-18

| Readable | k | p50 ms | p95 ms | fewer than k | recall vs exact |
|---|---|---|---|---|---|
| 90% | 8 | 1046.2 | 1075.2 | 0/50 | 1.000 |
| 90% | 50 | 1053.2 | 1087.0 | 0/50 | 1.000 |
| 10% | 8 | 54.6 | 57.6 | 0/50 | 1.000 |
| 10% | 50 | 62.4 | 65.0 | 0/50 | 1.000 |
| 1% | 8 | 8.8 | 9.1 | 0/50 | 1.000 |
| 1% | 50 | 17.1 | 19.0 | 0/50 | 1.000 |

## Findings

- **The production query does not use the HNSW index here.** `EXPLAIN` of `PgVectorStore.search` for the 90 percent user shows a sequential scan of `chunks`, hash joins with `documents` and `containers`, and a sort on the distance. The planner estimates that one row survives the join conditions (`chunks.container_id = documents.container_id` and `chunks.version_id = documents.current_version_id`), so sorting everything looks cheap.
  - The cost grows with the number of readable chunks: about 1 s at 54,000 readable chunks on this machine. Extrapolated, a user who can read most of a 1-million-chunk corpus would wait more than 15 s.
  - Recall is 1.000 in every row only because both sides are exact scans.
  - Users with narrow access are fast (9 ms at 1 percent) for the same reason: few rows to sort.
  - **Fix in R16:** reshape the query so that an ordered index scan on `chunks.embedding` drives it (filters on columns of `chunks` only, documents joined afterwards for the top k), and re-measure. The plan for selective users (exact scan below a threshold) is then decided on these numbers.
- **Size limit on this machine.** The HNSW build needs the whole graph in `maintenance_work_mem`: about 4.5 KB per 1024-dimension vector. The Compose Postgres has a 1 GB memory limit and 64 MB of `/dev/shm`, so 60,000 chunks is the largest corpus that builds in seconds here. 200,000 chunks fell back to the slow on-disk build and did not finish. Measuring at 1 million chunks or more needs a host with at least 6 GB for Postgres (or `halfvec`, R18).

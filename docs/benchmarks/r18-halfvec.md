# halfvec recall and the hybrid search on the evaluation set (R18)

Measured 2026-11-18 on the `acme` set (247 runs: 146 answerable, 101 unanswerable; every question asked as each
persona), strict mode (recorded bge-m3 embeddings, no Ollama), PostgreSQL 16.15, pgvector 0.8.7.

## halfvec against float32

Same retrieval as the R13 baseline (vector leg only, no reranker), before and after migration 0019 moved
`chunks.embedding` from `vector(1024)` to `halfvec(1024)`:

| metric | float32 (baseline) | halfvec | delta |
|---|---|---|---|
| recall@5 | 0.973 | 0.973 | 0.000 |
| recall@8 | 0.980 | 0.980 | 0.000 |
| MRR | 0.899 | 0.899 | 0.000 |
| nDCG@10 | 0.921 | 0.921 | 0.000 |
| abstention | 0.307 | 0.307 | 0.000 |
| leaks | 0 | 0 | 0 |

No measurable change on this set, at half the storage and index size (2 bytes per dimension instead of 4).
At 20 million chunks of 1,024 dimensions that is about 41 GB of vectors instead of 82 GB, and an HNSW build
that fits in about half the `maintenance_work_mem`.

Reproduce: `erag eval prepare`, then
`erag eval run --retrieval mode=vector --retrieval reranker= --json /tmp/halfvec.json` and
`erag eval compare eval/baselines/acme.json /tmp/halfvec.json`.

## Current default retrieval (hybrid, reranker not installed on this machine)

`erag eval gate` with the defaults of Admin > Retrieval (hybrid, `mmarco-minilm-l12` configured but not
installed, so every run used the fused order: 247 reranker fallbacks):

| metric | baseline | hybrid (fallback) |
|---|---|---|
| recall@8 | 0.980 | 1.000 |
| MRR | 0.899 | 0.983 |
| nDCG@10 | 0.921 | 0.988 |
| abstention | 0.307 | 0.287 |
| leaks | 0 | 0 |

The gate passes (max drop 0.02). Abstention on unanswerable questions is 0.02 lower: the full-text leg finds
something lexically close more often. The relative full-text floor added in R16 (`text_relative_floor`) keeps
it within the gate; the reranker, once installed (`erag models install mmarco-minilm-l12`), is the intended
answer to it.

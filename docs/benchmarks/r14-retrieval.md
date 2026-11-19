# Retrieval quality: vector, hybrid, hybrid + reranker (R14, measured 2026-11-19)

Set `acme`: 247 runs (146 answerable, 101 unanswerable or forbidden), every question asked as each persona, strict mode (recorded `bge-m3` embeddings). PostgreSQL 16.15, pgvector 0.8.7, `halfvec` embeddings. Hardware: AMD Ryzen 3 3100 (4 cores, 8 threads), 16 GB RAM, no GPU. Reranker `mmarco-minilm-l12` (int8 ONNX, installed with `erag models install`), 30 candidates.

This file was cited by ADR 0047 before it existed; the numbers below are the first recorded measurement of the default pipeline.

| Pipeline | recall@5 | recall@8 | MRR | nDCG@10 | abstention | leaks | search p50 / p95 |
|---|---|---|---|---|---|---|---|
| Vector only (R13 baseline, float32) | 0.973 | 0.980 | 0.899 | 0.921 | 0.307 | 0 | 7 / 11 ms |
| Vector only, `halfvec` | 0.973 | 0.980 | 0.899 | 0.921 | 0.307 | 0 | 8 / 12 ms |
| Hybrid, no reranker (fallback) | 1.000 | 1.000 | 0.983 | 0.988 | 0.287 | 0 | |
| **Hybrid + reranker, floor 0.02 (default)** | 0.966 | 0.966 | 0.966 | 0.966 | **0.960** | 0 | 1,283 / 1,715 ms |
| Hybrid + reranker, floor 0.01 | 0.966 | 0.966 | 0.966 | 0.966 | 0.921 | 0 | 1,260 / 1,724 ms |

**Abstention** is the share of unanswerable runs (no answer for that persona) where retrieval returned nothing, so the chat says it has no information instead of answering from something merely similar.

## Reading

- The reranker is what makes the system **say "I don't know"**: abstention goes from 0.29-0.31 to 0.96. Without it, similarity search almost always returns something.
- It costs recall on **5 of 146** answerable runs: two paraphrases, an acronym and two cross-lingual questions (`hw-004`, `hw-005` as the new hire, `hw-041` as HR and the executive, `hw-061` as support). The right document is in the candidates but scores below the floor. Lowering the floor to 0.01 recovers none of them and lets more unanswerable questions through, so the default stays at **0.02**.
- MRR rises from 0.90 to 0.97: when the answer is found, it is first.
- **Latency on CPU is the cost**: about 1.3 s per search for 30 candidates, most of the retrieval time. On a GPU (`ERAG_RERANKER_USE_CUDA`) or with fewer candidates it is much lower. Without the reranker, hybrid search is the best recall here but abstains poorly.
- No configuration leaked anything.

Reproduce:

```
erag models install mmarco-minilm-l12
erag eval prepare
erag eval run --label hybrid-reranker
erag eval run --retrieval reranker= --label hybrid-only
erag eval run --retrieval mode=vector --retrieval reranker= --label vector
erag eval compare eval/baselines/acme.json <report>
```

# 0039. A cross-encoder reranker inside the application process

**Status:** Accepted (2026-11-18). The owner chose in process over a separate service.

## Decision
- **Runtime.** ONNX Runtime (MIT) and `tokenizers` (Apache-2.0) run a cross-encoder in every app replica: adding replicas adds reranking capacity, and no text ever leaves the process. Inference runs in the default thread pool (ONNX Runtime releases the GIL), at most `ERAG_RERANKER_CONCURRENCY` batches at a time per process. A request that cannot get a slot and an answer within `ERAG_RERANKER_TIMEOUT_SECONDS` falls back.
- **Models** (`app.ml.models`), each pinned by repository revision and the SHA-256 of every file:

  | Model | Size | License |
  |---|---|---|
  | `mmarco-minilm-l12` (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, int8 AVX2) | 129 MB | Apache-2.0 |
  | `bge-reranker-v2-m3` (onnx-community int8 export of the BAAI model) | 560 MB | Apache-2.0 |
  | `bge-m3-tokenizer`, used for exact token counts | 16 MB | MIT |

  **Nothing downloads automatically.** `erag models install <name>` fetches exactly the pinned bytes (or copies them with `--from-dir` for offline installations) and refuses a hash mismatch.
- **Default:** `mmarco-minilm-l12`, `rerank_candidates` 30, `rerank_min_score` 0.02. The choice was made on the evaluation set; numbers are in `docs/benchmarks/r14-retrieval.md`.
- **Degraded mode.** If the model is not installed, fails to load, or times out, retrieval uses the fused order with the no-reranker floors. The chat shows "Results were not reranked this time.", the audit entry records `reranker_fallback`, Admin > Retrieval shows the model state, and evaluation reports count fallbacks.

## Consequences
- The app needs more memory: about 300 MB with the small model and 1 GB with the large one. The Compose app limit rises to 2 GB.
- On CPU (Ryzen 3 3100) reranking 30 candidates takes about 1.3 s with the small model and about 3.6 s with the large one: most of the retrieval latency. A GPU (`ERAG_RERANKER_USE_CUDA`) or fewer candidates reduce it.
- CI installs the small model (cached), so the evaluation gate measures the default pipeline.

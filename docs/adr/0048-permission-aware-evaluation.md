# 0048. Permission-aware retrieval evaluation, recorded embeddings and a separate database

**Status:** Accepted (2026-11-18)

## Context
v0.5 changes retrieval: full text, fusion and a reranker (PLAN-v0.5 section 4). Without measurements, nobody can tell whether a change helps or hurts, and a retrieval change is also a new path to the data. The ACME demo (12 documents, about 950 words) is too small to tell configurations apart. GitHub CI has no Ollama, so it cannot compute `bge-m3` embeddings.

## Decision
- **Questions are asked as personas.** An item lists `as_users`, and the expectation depends on who asks: the documents that should come back, whether the question is answerable, and `must_not_see`. Every forbidden question runs as an authorised and an unauthorised persona. The run uses the production path: `effective_permissions` and `Retriever.retrieve_by_vector`, with `retrieval_top_k` and `retrieval_min_score`.
- **An independent oracle.** Every retrieved document is also checked against the *declared* structure (`app.eval.corpus.Oracle`, built from the seed specs), not against `app.authz`. A leak is caught even if nobody wrote it down in `must_not_see`, and even if the policy code and the seed agree on something wrong. **Any leak fails the run, whatever the other numbers.**
- **Metrics at document level:**
  - recall@5 and recall@8, MRR and nDCG@10;
  - abstention on unanswerable runs (nothing above `min_score` came back);
  - per-tag recall and MRR;
  - p50/p95 latency.
  A document's rank is the rank of its first chunk, so recall@8 means "the chat would have it in its context".
- **An evaluation corpus apart from the demo.** `app.eval.corpus` generates about 250 documents deterministically:
  - contracts with near-duplicates;
  - support articles in Spanish and English;
  - product sheets with near-identical specifications;
  - invoices and two more restricted HR files.
  It also generates about 100 questions about exact identifiers. The output is committed and a test checks that it regenerates byte for byte. About 50 more questions are written by hand. The demo is unchanged.
- **Recorded real embeddings.** `eval/cache/bge-m3.bin` holds the real `bge-m3` vectors of every chunk text and question, keyed by `sha256(model, text)` and stored as float16. Every vector, live or recorded, goes through the same rounding, so a run with Ollama and a run from the file give identical numbers (verified).
  - They are outputs of the real model, not a fake provider (ADR 0008).
  - A text missing from the file is an error in strict mode, never a guess.
  - `erag eval embed-cache` re-records the file and drops unused entries.
- **A separate database.** The evaluation creates personas and confidential documents, so it runs on `ERAG_EVAL_DATABASE_URL` (default `erag_eval` on the app's server). Every eval and benchmark command refuses to run when that database is the application's. Personas get a random, discarded password; blobs go to a temporary directory.
- **Storage.** Runs are stored in `eval_sets`, `eval_items`, `eval_runs` (with a configuration snapshot and the commit) and `eval_results` (document keys and scores, **never text**). R15 will use the same tables from the admin area. Reports are JSON files; `eval/baselines/acme.json` is committed.
- **CI gate:** `erag eval prepare && erag eval gate`. It fails on any leak, or on a drop of more than 0.02 in recall@8 or MRR against the baseline. Updating the baseline is a deliberate commit (`erag eval baseline`).

## Consequences
- Changing the chunker, the parser, the embedding input or the corpus requires re-recording the embeddings (`erag eval embed-cache`, needs Ollama) before CI can pass. The error message says so.
- The cache adds about 1.7 MB to the repository for each re-recording. If it grows, it can move to a CI cache keyed by its hash.
- The gate measures retrieval only. Answer quality (citations, faithfulness) needs a chat model and stays outside the gate (R15).
- The synthetic benchmark (`scripts/synth_corpus.py`, `scripts/bench_search.py`) uses the same database guard. It measures latency and recall at scale with synthetic vectors, which say nothing about answer quality.
- Tests: `tests/eval/` (metrics, set validation, generator stability, the recorded embeddings, the database guard, and an end-to-end run where a planted grant makes the gate fail).

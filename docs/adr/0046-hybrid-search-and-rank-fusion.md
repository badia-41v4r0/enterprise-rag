# 0046. Hybrid search and rank fusion, with one authorization clause for both legs

**Status:** Accepted (2026-11-18)

## Context
Vector search alone misses exact identifiers (contract numbers, error codes, SKUs) and is poor at saying "nothing relevant here". The R13 evaluation measured both: MRR 0.57 to 0.76 on Spanish contract identifiers, and abstention 0.307. A second retrieval path is also a second path to the data.

## Decision
- **Full-text leg in Postgres.** Every chunk gets `lang`, a `tsvector` and a GIN index (migration 0014). The `tsvector` combines:
  - the title (weight A);
  - the heading and text in the document's language (`spanish` or `english`, detected by stop-word counts);
  - the same text again with `simple`, so identifiers match exactly.

  `unaccent` is applied on both sides. A question becomes an OR query of its stemmed Spanish and English terms plus every token containing a digit, ranked with `ts_rank_cd` (normalization 1|32).
- **One authorization clause for both legs.**
  - `chunk_access_clause` (container only, on `chunks`) filters the inner candidate query of both legs.
  - `document_access_clause` (same container, current version, restricted documents only with a grant) filters the joined document.

  Neither leg can be built without them. The ACL recheck then runs on the fused set before any text reaches the reranker.
- **Fusion:** reciprocal rank fusion, `sum(weight / (k + rank))` with k = 60 and both weights 1.0, so cosine and `ts_rank_cd` never have to be put on one scale.
- **Runtime settings** (`app_settings.retrieval`, Admin > Retrieval): mode, candidates per leg, fusion constant and weights, reranker and its floor, the no-reranker floors, and final k. They change ranking only. Every change is audited with old and new values. Evaluation runs override them per run (`erag eval run --retrieval key=value`) and record them in the run's snapshot.
- **Index-driven query** (also R16): the inner query filters `chunks` columns only, so the HNSW scan (vector leg) or the GIN index (text leg) drives it. Documents rejected afterwards are compensated by over-fetching, up to 2,000 candidates.

## Consequences
- On the `acme` set, hybrid search **without** a reranker is slightly worse than vector search (MRR 0.886 against 0.899, abstention 0.198 against 0.307): fusion lets more weak candidates through. Its value shows with the reranker (ADR 0047), which needs good candidates from both legs.
- Existing chunks get a language-neutral `tsvector` from the migration. `erag reindex --all` recomputes them per language.
- Tests: `tests/security/test_hybrid_retrieval.py`:
  - a forbidden exact identifier is found only by the authorized user;
  - a property test over random users and permissions checks every hit of each leg and of the full pipeline;
  - the reranker fallback does not widen anything;
  - the settings API is admin-only, validated and audited.

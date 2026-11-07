# 0002. ACL resolved at query time and pushed into the vector query

**Status:** Accepted (2026-11-08)

## Context
A common RAG design copies ACLs into vector payloads at indexing time. Revocation then needs a re-index, and stale payloads leak.

## Decision
- Chunks carry only location: `container_id`, `document_id`, `version_id`. No ACL data.
- For every request, `authz.effective_permissions(user)` computes, from PostgreSQL, the set of readable container IDs and the set of restricted documents the user may read.
- The pgvector query joins `documents` and applies the filter **in SQL** (`container_id = ANY(:allowed)` and `NOT restricted OR document_id = ANY(:restricted_allowed)` and `version_id = current_version_id`). Results are never post-filtered from an unfiltered top-k. HNSW iterative scans (`hnsw.iterative_scan`, pgvector >= 0.8) keep recall when the filter is selective.
- After search, every hit is re-checked against Postgres with the same `can_read_document` policy (defense in depth). A mismatch drops the chunk and writes an `acl_recheck_mismatch` audit entry.
- Conversation history is re-validated too: an earlier answer whose cited documents are no longer readable is withheld from the prompt.

## Consequences
Revocation is immediate, with no re-index (tested). The filter costs a join per query, which is negligible at MVP scale. A Qdrant adapter would need the allowed set passed as a payload filter on `container_id`/`document_id`, which the interface already supports.

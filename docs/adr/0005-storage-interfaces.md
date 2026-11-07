# 0005. `VectorStore` and `BlobStore` interfaces

**Status:** Accepted (2026-11-08)

## Decision
- `BlobStore` (`put`, `get`, `delete`, `exists`), content-addressed by SHA-256. Implementation: `LocalBlobStore` on a volume (`ERAG_BLOB_DIR`). S3/MinIO later.
- `VectorStore` (`upsert_chunks`, `delete_version`, `search(query_vector, access_filter, k)`). Implementation: `PgVectorStore`. The `AccessFilter` is a required argument of `search`: there is no unfiltered search method.

## Consequences
Swapping storage needs a new adapter, not changes in retrieval or ingestion.

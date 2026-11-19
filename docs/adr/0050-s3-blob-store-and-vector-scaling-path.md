# 0050. S3 blob store, halfvec, and the vector-store scaling path

**Status:** Accepted (2026-11-18).

## Decision
- **S3-compatible blob store** (`ERAG_BLOB_BACKEND=s3`): AWS S3, Ceph RGW, SeaweedFS, or Azure through a gateway. Objects are named by SHA-256 as before (dedupe, immutability), path-style addressing, server-side encryption requested on every write, the hash checked on every read (corruption or tampering is an error, never silently served). Requests are signed with SigV4 implemented on httpx: no SDK dependency; the signer is checked against AWS's published example. Credentials come from the environment, like the database password.
- `erag blobs migrate --to s3` copies an installation's local blobs, verifying each hash; it is idempotent and leaves the local files in place.
- Tested against **SeaweedFS** (Apache-2.0). MinIO is AGPL and excluded (D8).
- **halfvec.** Embeddings are stored as `halfvec(1024)` (migration 0019): half the storage and index memory, identical recall, MRR and nDCG on the evaluation set (`docs/benchmarks/r18-halfvec.md`). The HNSW rebuild during the migration is long on large installations: run it in a maintenance window with a raised `maintenance_work_mem`.
- **Beyond pgvector**: the `VectorStore` interface would take a Qdrant adapter (payload = location only, never ACLs; outbox writes; Postgres recheck kept). Built only if pgvector misses the target in the benchmark (decision 8); not built in v0.5.

## Consequences
- Blob storage scales independently of the app hosts, and app replicas no longer need a shared volume.
- One more set of credentials to manage and back up (ADR 0051).

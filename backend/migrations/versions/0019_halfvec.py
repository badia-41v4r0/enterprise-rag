"""Embeddings as halfvec (R18, PLAN-v0.5 5.3): half the storage and index size, for 20 million chunks

The HNSW index is rebuilt with `halfvec_cosine_ops`. On a large installation this is a long, locking
operation: run it in a maintenance window, with `maintenance_work_mem` raised and
`max_parallel_maintenance_workers` set (docs/operations/capacity.md). Recall is checked on the evaluation set
(docs/benchmarks/r18-halfvec.md).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-18 22:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIM = 1024

# Not expand/contract: the column type changes in place, so a replica of the previous release (which binds
# `vector` parameters) cannot run against the new schema. Upgrade with the app stopped (docs/operations).
REQUIRES_DOWNTIME = "chunks.embedding changes type (vector -> halfvec) and the HNSW index is rebuilt"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE halfvec({DIM}) USING embedding::halfvec({DIM})")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({DIM}) USING embedding::vector({DIM})")
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")

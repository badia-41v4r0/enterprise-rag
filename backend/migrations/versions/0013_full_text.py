"""Full-text leg of hybrid search: language and tsvector per chunk with a GIN index, chunker version (R14)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.add_column("chunks", sa.Column("lang", sa.String(16), nullable=False, server_default="simple"))
    op.add_column("chunks", sa.Column("tsv", postgresql.TSVECTOR(), nullable=True))
    op.add_column("chunks", sa.Column("chunker_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    # Existing chunks: language-neutral terms until `erag reindex` recomputes them per language.
    op.execute(
        "UPDATE chunks c SET tsv = setweight(to_tsvector('simple', unaccent(d.title)), 'A') "
        "|| to_tsvector('simple', unaccent(c.text)) FROM documents d WHERE d.id = c.document_id"
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_column("chunks", "chunker_version")
    op.drop_column("chunks", "tsv")
    op.drop_column("chunks", "lang")

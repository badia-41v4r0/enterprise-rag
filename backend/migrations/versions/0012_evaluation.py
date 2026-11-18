"""Permission-aware evaluation: sets, items, runs and per-persona results (ADR 0040)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLS = postgresql.ENUM(name="classification", create_type=False)
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "eval_sets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("source", sa.String(10), nullable=False, server_default="repo"),
        sa.Column("classification", CLS, nullable=False, server_default="confidential"),
        sa.Column("digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "eval_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("set_id", UUID, sa.ForeignKey("eval_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY),
        sa.UniqueConstraint("set_id", "key", name="uq_eval_item_set_key"),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("set_id", UUID, sa.ForeignKey("eval_sets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("metrics", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "eval_results",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("run_id", UUID, sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("item_key", sa.String(200), nullable=False),
        sa.Column("persona", sa.String(320), nullable=False),
        sa.Column("retrieved", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("metrics", JSONB, nullable=False, server_default=EMPTY),
        sa.Column("leaked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("embed_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("search_ms", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
    op.drop_table("eval_items")
    op.drop_table("eval_sets")

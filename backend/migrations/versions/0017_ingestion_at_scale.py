"""Ingestion at scale (R18): job priority, backoff, heartbeat, dead letters; sync run heartbeat

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-18 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'dead'")
    op.add_column("ingestion_jobs", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingestion_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    # The claim query: queued jobs by priority then age.
    op.create_index(
        "ix_ingestion_jobs_claim",
        "ingestion_jobs",
        [sa.text("priority DESC"), "created_at"],
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.add_column("sync_runs", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_runs", "locked_until")
    op.drop_index("ix_ingestion_jobs_claim", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "locked_until")
    op.drop_column("ingestion_jobs", "next_attempt_at")
    op.drop_column("ingestion_jobs", "priority")
    # Postgres cannot drop an enum value; 'dead' jobs are re-queued so nothing uses it.
    op.execute("UPDATE ingestion_jobs SET state = 'failed' WHERE state = 'dead'")

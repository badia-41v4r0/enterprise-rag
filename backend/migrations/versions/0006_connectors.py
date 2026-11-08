"""connectors, connector items and sync runs (R3)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column(
            "container_id", sa.UUID(), sa.ForeignKey("containers.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("secret_refs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("schedule_minutes", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    op.create_table(
        "connector_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "connector_id", sa.UUID(), sa.ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("external_id", sa.String(2000), nullable=False),
        sa.Column(
            "document_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
        ),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("version_hint", sa.String(500), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.UniqueConstraint("connector_id", "external_id", name="uq_connector_item"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "connector_id", sa.UUID(), sa.ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued", index=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("triggered_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("counts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("connector_items")
    op.drop_table("connectors")

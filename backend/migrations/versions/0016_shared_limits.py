"""Shared limits and the generation queue (R17): rate-limit counters, budget reservations, endpoint pools,
generation leases and waiters, role priority

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-18 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # UNLOGGED: not WAL-logged, emptied after a crash. Acceptable for rate limits, never used for money.
    op.execute(
        """
        CREATE UNLOGGED TABLE rate_limit_counters (
            key varchar(300) NOT NULL,
            window_start bigint NOT NULL,
            hits integer NOT NULL DEFAULT 0,
            PRIMARY KEY (key, window_start)
        )
        """
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(200), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_budget_reservations_request_id", "budget_reservations", ["request_id"])
    op.create_index("ix_budget_reservations_scope", "budget_reservations", ["scope"])
    op.create_index("ix_budget_reservations_expires_at", "budget_reservations", ["expires_at"])

    op.create_table(
        "llm_endpoints",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id", UUID(as_uuid=True), sa.ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("base_url", sa.String(1000), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider_id", "base_url", name="uq_llm_endpoint_url"),
    )
    op.create_index("ix_llm_endpoints_provider_id", "llm_endpoints", ["provider_id"])
    op.add_column("llm_providers", sa.Column("max_concurrency", sa.Integer(), nullable=True))
    op.add_column("roles", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "generation_leases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("pool", sa.String(100), nullable=False),
        sa.Column("endpoint", sa.String(1000), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_generation_leases_pool", "generation_leases", ["pool"])
    op.create_table(
        "generation_waiters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("pool", sa.String(100), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_generation_waiters_pool", "generation_waiters", ["pool"])


def downgrade() -> None:
    op.drop_table("generation_waiters")
    op.drop_table("generation_leases")
    op.drop_column("roles", "priority")
    op.drop_column("llm_providers", "max_concurrency")
    op.drop_table("llm_endpoints")
    op.drop_table("budget_reservations")
    op.drop_table("rate_limit_counters")

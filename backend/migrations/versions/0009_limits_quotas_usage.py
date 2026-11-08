"""Cost limits, prices, token accounting and quotas per user and role (PLAN-v0.3 R6)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIMIT_COLUMNS = (
    ("max_output_tokens", sa.Integer()),
    ("max_input_tokens", sa.Integer()),
    ("tokens_per_day", sa.BigInteger()),
    ("tokens_per_month", sa.BigInteger()),
)


def upgrade() -> None:
    for name, type_ in LIMIT_COLUMNS:
        op.add_column("llm_providers", sa.Column(name, type_, nullable=True))
        op.add_column("llm_models", sa.Column(name, type_, nullable=True))
    op.add_column("llm_providers", sa.Column("max_turn_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_models", sa.Column("monthly_budget_usd", sa.Float(), nullable=True))
    # Prices in USD per million tokens; NULL = unknown (treated as 0 by the worst-case check, flagged in the UI).
    op.add_column("llm_models", sa.Column("input_usd_per_mtok", sa.Float(), nullable=True))
    op.add_column("llm_models", sa.Column("output_usd_per_mtok", sa.Float(), nullable=True))
    op.add_column("llm_models", sa.Column("price_source", sa.String(20), nullable=True))

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("estimated_requests", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "uq_llm_usage_key",
        "llm_usage",
        ["provider", "model", "user_id", "day"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_llm_usage_user_month", "llm_usage", ["user_id", "month"])
    op.create_index("ix_llm_usage_provider_month", "llm_usage", ["provider", "month"])

    op.create_table(
        "quotas",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("subject", sa.String(10), nullable=False),  # "user" | "role"
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("role_id", sa.UUID(), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("kind", sa.String(12), nullable=False),  # "standard" | "collective"
        sa.Column("provider", sa.String(40), nullable=True),  # NULL = every provider
        sa.Column("tokens_per_day", sa.BigInteger(), nullable=True),
        sa.Column("tokens_per_month", sa.BigInteger(), nullable=True),
        sa.Column("usd_per_day", sa.Float(), nullable=True),
        sa.Column("usd_per_month", sa.Float(), nullable=True),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(subject = 'user' AND user_id IS NOT NULL AND role_id IS NULL AND kind = 'standard') OR "
            "(subject = 'role' AND role_id IS NOT NULL AND user_id IS NULL AND kind IN ('standard', 'collective'))",
            name="ck_quota_subject",
        ),
    )
    op.create_index(
        "uq_quota_scope",
        "quotas",
        ["subject", "user_id", "role_id", "kind", "provider"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "quota_pool_usage",
        sa.Column("quota_id", sa.UUID(), sa.ForeignKey("quotas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),  # "YYYY-MM-DD" or "YYYY-MM"
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("quota_id", "period"),
    )


def downgrade() -> None:
    op.drop_table("quota_pool_usage")
    op.drop_index("uq_quota_scope", table_name="quotas")
    op.drop_table("quotas")
    op.drop_index("ix_llm_usage_provider_month", table_name="llm_usage")
    op.drop_index("ix_llm_usage_user_month", table_name="llm_usage")
    op.drop_index("uq_llm_usage_key", table_name="llm_usage")
    op.drop_table("llm_usage")
    for name in ("price_source", "output_usd_per_mtok", "input_usd_per_mtok", "monthly_budget_usd"):
        op.drop_column("llm_models", name)
    op.drop_column("llm_providers", "max_turn_tokens")
    for name, _ in LIMIT_COLUMNS:
        op.drop_column("llm_models", name)
        op.drop_column("llm_providers", name)

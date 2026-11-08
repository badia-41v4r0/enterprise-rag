"""LLM provider registry: providers, models, the default model and a daily request counter

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLS = postgresql.ENUM(name="classification", create_type=False)


def upgrade() -> None:
    op.create_table(
        "llm_providers",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("slug", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("base_url", sa.String(1000), nullable=False),
        sa.Column("self_hosted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("locality", sa.String(10), nullable=False, server_default="cloud"),
        sa.Column("max_classification", CLS, nullable=True),
        sa.Column("secret_name", sa.String(100), nullable=True),
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
        sa.Column("daily_request_limit", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "llm_models",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "provider_id", sa.UUID(), sa.ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False, index=True
        ),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("env_managed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider_id", "model", name="uq_llm_model_provider_model"),
    )
    # At most one default model for the whole installation.
    op.create_index(
        "uq_llm_models_single_default",
        "llm_models",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "provider_daily_usage",
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("provider", "day"),
    )


def downgrade() -> None:
    op.drop_table("provider_daily_usage")
    op.drop_index("uq_llm_models_single_default", table_name="llm_models")
    op.drop_table("llm_models")
    op.drop_table("llm_providers")

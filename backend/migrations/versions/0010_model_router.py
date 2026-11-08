"""Model router: deciders, lanes, decision log; tool capability per model (PLAN-v0.3 R7 to R9)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 03:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLS = postgresql.ENUM(name="classification", create_type=False)


def upgrade() -> None:
    # Whether a model may be offered tools (the router only picks tool-capable models when tools are offered).
    op.add_column(
        "llm_models", sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default=sa.text("true"))
    )
    op.create_table(
        "router_deciders",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        # "prompted" | "protocol" (open Jev-style server) | "jev_official"
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("base_url", sa.String(1000), nullable=True),
        sa.Column("model", sa.String(250), nullable=True),
        sa.Column("secret_name", sa.String(100), nullable=True),
        sa.Column("self_hosted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("locality", sa.String(10), nullable=False, server_default="cloud"),
        sa.Column("max_classification", CLS, nullable=True),
        sa.Column("see_context", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("timeout_ms", sa.Integer(), nullable=True),
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
        sa.Column("daily_request_cap", sa.Integer(), nullable=True),
        sa.Column("input_usd_per_mtok", sa.Float(), nullable=True),
        sa.Column("output_usd_per_mtok", sa.Float(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "router_lanes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("conditions", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("targets", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_router_lanes_single_default",
        "router_lanes",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    # Never the question nor any context: classification, sets of model ids, answers, choice and figures.
    op.create_table(
        "router_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("classification", CLS, nullable=False),
        sa.Column("tools_offered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("permitted", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decider", sa.String(100), nullable=False),
        sa.Column("attempts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("lane", sa.String(100), nullable=True),
        sa.Column("chosen_model", sa.String(250), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("router_decisions")
    op.drop_index("uq_router_lanes_single_default", table_name="router_lanes")
    op.drop_table("router_lanes")
    op.drop_table("router_deciders")
    op.drop_column("llm_models", "supports_tools")

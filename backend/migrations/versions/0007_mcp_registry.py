"""MCP registry: remote servers, clearance, tool review and argument constraints (R4)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLS = postgresql.ENUM(name="classification", create_type=False)


def upgrade() -> None:
    postgresql.ENUM("pending", "approved", "disabled", name="mcp_server_status").create(op.get_bind(), checkfirst=True)
    status = postgresql.ENUM(name="mcp_server_status", create_type=False)
    add = op.add_column
    add("mcp_servers", sa.Column("builtin", sa.Boolean(), nullable=False, server_default="true"))
    add("mcp_servers", sa.Column("transport", sa.String(20), nullable=False, server_default="stdio"))
    add("mcp_servers", sa.Column("url", sa.String(1000), nullable=True))
    add("mcp_servers", sa.Column("status", status, nullable=False, server_default="approved"))
    add("mcp_servers", sa.Column("description", sa.Text(), nullable=False, server_default=""))
    add("mcp_servers", sa.Column("auth_secret", sa.String(100), nullable=True))
    add("mcp_servers", sa.Column("max_classification", CLS, nullable=False, server_default="confidential"))
    add("mcp_servers", sa.Column("result_classification", CLS, nullable=False, server_default="public"))
    add("mcp_servers", sa.Column("send_permission_context", sa.Boolean(), nullable=False, server_default="true"))
    add("mcp_servers", sa.Column("limits", postgresql.JSONB(), nullable=False, server_default="{}"))
    add("mcp_servers", sa.Column("runtime", postgresql.JSONB(), nullable=False, server_default="{}"))
    add("mcp_servers", sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True))
    add(
        "mcp_servers",
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    add("tools", sa.Column("content_hash", sa.String(64), nullable=True))
    add("tools", sa.Column("approved_hash", sa.String(64), nullable=True))
    add("tools", sa.Column("needs_review", sa.Boolean(), nullable=False, server_default="false"))
    add("tools", sa.Column("review_reason", sa.Text(), nullable=True))
    add("tools", sa.Column("arg_constraints", postgresql.JSONB(), nullable=False, server_default="{}"))


def downgrade() -> None:
    for col in ("arg_constraints", "review_reason", "needs_review", "approved_hash", "content_hash"):
        op.drop_column("tools", col)
    for col in (
        "created_by",
        "last_discovered_at",
        "runtime",
        "limits",
        "send_permission_context",
        "result_classification",
        "max_classification",
        "auth_secret",
        "description",
        "status",
        "url",
        "transport",
        "builtin",
    ):
        op.drop_column("mcp_servers", col)
    postgresql.ENUM(name="mcp_server_status").drop(op.get_bind(), checkfirst=True)

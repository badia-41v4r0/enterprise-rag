"""Many users (R16): permission-cache epoch, last login, session activity

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-18 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authz_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute("INSERT INTO authz_state (id, epoch) VALUES (1, 0)")
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_last_login_at", "users", ["last_login_at"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_index("ix_audit_log_action_ts", "audit_log", ["action", "ts"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_action_ts", table_name="audit_log")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_users_last_login_at", table_name="users")
    op.drop_column("sessions", "last_seen_at")
    op.drop_column("users", "last_login_at")
    op.drop_table("authz_state")

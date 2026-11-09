"""Guided first-run setup: setup_state (singleton) and users.must_change_password

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12 05:00:00.000000

An existing installation is never put into setup mode by this upgrade: when the database already has users, a
completed setup record is written (completed_via = "upgrade"). A fresh database gets no row here; `erag bootstrap`
creates the setup-mode row (and the setup token) at the first start (ADR 0034).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.create_table(
        "setup_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_hash", sa.String(64), nullable=True),
        sa.Column("session_csrf", sa.String(64), nullable=True),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("license_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("license_accepted_by", sa.String(320), nullable=True),
        sa.Column("license_text_sha256", sa.String(64), nullable=True),
        sa.Column("install_type", sa.String(20), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_via", sa.String(20), nullable=True),
        sa.Column("checklist_dismissed", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_setup_state_singleton"),
    )
    # Invariant: an upgrade never puts an existing installation into setup mode.
    op.execute(
        """
        INSERT INTO setup_state (id, steps, completed_at, completed_via, install_type)
        SELECT 1, '{}'::jsonb, now(), 'upgrade',
               CASE WHEN EXISTS (SELECT 1 FROM users WHERE email LIKE '%@acme.test') THEN 'demo' ELSE NULL END
        WHERE EXISTS (SELECT 1 FROM users)
        """
    )


def downgrade() -> None:
    op.drop_table("setup_state")
    op.drop_column("users", "must_change_password")

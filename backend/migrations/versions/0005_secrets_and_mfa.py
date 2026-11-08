"""credential store, runtime settings, MFA and step-up (R2)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_by", sa.String(200), nullable=True),
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
    )
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("mfa_secret_ct", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("mfa_secret_nonce", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("mfa_key_id", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sessions", sa.Column("mfa_pending", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("sessions", sa.Column("mfa_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sessions", sa.Column("reauth_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("reauth_at", "mfa_attempts", "mfa_pending"):
        op.drop_column("sessions", col)
    for col in ("mfa_last_step", "mfa_key_id", "mfa_secret_nonce", "mfa_secret_ct", "mfa_enabled"):
        op.drop_column("users", col)
    op.drop_table("mfa_recovery_codes")
    op.drop_table("app_settings")
    op.drop_table("secrets")

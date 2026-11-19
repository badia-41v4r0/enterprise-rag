"""Observability (R19): the correlation id on audit entries

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-19 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("correlation_id", sa.String(64), nullable=True))
    op.create_index("ix_audit_log_correlation_id", "audit_log", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_correlation_id", table_name="audit_log")
    op.drop_column("audit_log", "correlation_id")

"""provider spend ceiling

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_spend",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("provider", "month"),
    )


def downgrade() -> None:
    op.drop_table("provider_spend")

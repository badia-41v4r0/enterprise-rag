"""Per-model clearance (can only lower the provider's) and per-model request options (OpenRouter Jev Router)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11 05:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLS = postgresql.ENUM(name="classification", create_type=False)


def upgrade() -> None:
    op.add_column("llm_models", sa.Column("max_classification", CLS, nullable=True))
    op.add_column(
        "llm_models", sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"))
    )


def downgrade() -> None:
    op.drop_column("llm_models", "options")
    op.drop_column("llm_models", "max_classification")

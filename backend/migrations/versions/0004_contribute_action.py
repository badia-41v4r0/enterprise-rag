"""grant action `contribute` (R1: delegated content management)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE grant_action ADD VALUE IF NOT EXISTS 'contribute' BEFORE 'manage'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value: rebuild the type without it.
    op.execute("DELETE FROM grants WHERE action = 'contribute'")
    op.execute("ALTER TYPE grant_action RENAME TO grant_action_old")
    op.execute("CREATE TYPE grant_action AS ENUM ('read', 'manage', 'execute')")
    op.execute("ALTER TABLE grants ALTER COLUMN action TYPE grant_action USING action::text::grant_action")
    op.execute("DROP TYPE grant_action_old")

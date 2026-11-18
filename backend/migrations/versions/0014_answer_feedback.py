"""Answer feedback (thumbs up/down) and its review into evaluation items (R15)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-18 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("message_id", UUID, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("review_state", sa.String(20), nullable=True, index=True),
        sa.Column("reviewed_by", UUID, nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eval_item_id", UUID, sa.ForeignKey("eval_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
    )


def downgrade() -> None:
    op.drop_table("answer_feedback")

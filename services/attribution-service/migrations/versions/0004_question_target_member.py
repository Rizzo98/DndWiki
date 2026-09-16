"""review questions carry the member they are about

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-05

A presence question ("was Hann there?") is about a MEMBER across a stretch, and
the options are yes/no rather than a roster list - so the member cannot be read
off the option keys the way a content question's answer can. Without this column
the answer would arrive at the engine with nothing to apply it to.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_questions", sa.Column("target_member", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("review_questions", "target_member")

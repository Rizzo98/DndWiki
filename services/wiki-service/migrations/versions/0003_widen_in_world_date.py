"""widen timeline event in-world dates

The LLM extraction produces verbose campaign calendar strings (e.g.
"33esimo giorno del primo mese dell'anno della settima era") that exceed
the original 64-char limit and fail validation with a 422. Widen the
column (and the matching Pydantic schemas) to 256 chars.

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "timeline_events",
        "in_world_date",
        existing_type=sa.String(length=64),
        type_=sa.String(length=256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "timeline_events",
        "in_world_date",
        existing_type=sa.String(length=256),
        type_=sa.String(length=64),
        existing_nullable=True,
    )

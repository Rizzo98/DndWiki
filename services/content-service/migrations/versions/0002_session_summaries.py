"""add session_summaries

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("generation_job_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("characters", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("locations", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("events", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "timeline_entries", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("llm_provider", sa.String(length=64), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_session_summaries_session_id", "session_summaries", ["session_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_session_summaries_session_id", table_name="session_summaries")
    op.drop_table("session_summaries")

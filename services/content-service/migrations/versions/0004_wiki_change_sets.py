"""wiki change sets: the proposed-changes review layer

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-03

The confirmed session summary is no longer written to the wiki directly: it is
turned into a PROPOSED change set (pages to create, pages to update, timeline
entries to write, cross-references to propose) that the DM inspects, edits and
confirms on the session page - the 'git status' of the session. This table
stores that set per session.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_change_sets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("summary_id", sa.Uuid(), nullable=True),
        sa.Column("generation_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="draft"
        ),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("changes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("relations", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skipped", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_wiki_change_sets_session_id", "wiki_change_sets", ["session_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_change_sets_session_id", table_name="wiki_change_sets")
    op.drop_table("wiki_change_sets")

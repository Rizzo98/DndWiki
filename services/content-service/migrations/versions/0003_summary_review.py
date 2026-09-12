"""session summary review layer: draft/confirm columns + job phase

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-02

The session summary became the reviewable intermediate layer between the
transcript and the wiki: it is written as a draft, the DM edits/regenerates
it, and only the confirmed summary is materialized into wiki pages and
timeline events. This revision adds the review bookkeeping to
'session_summaries' (review_status/revision/confirmed_*/edit_history), the
data the wiki phase needs to rebuild drafts without re-reading the transcript
(language/party_characters), and the phase marker on 'generation_jobs'.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_summaries", sa.Column("language", sa.String(length=16), nullable=True))
    op.add_column(
        "session_summaries",
        sa.Column(
            "party_characters", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "session_summaries",
        sa.Column(
            "review_status", sa.String(length=16), nullable=False, server_default="confirmed"
        ),
    )
    op.add_column(
        "session_summaries",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "session_summaries",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("session_summaries", sa.Column("confirmed_by", sa.Uuid(), nullable=True))
    op.add_column(
        "session_summaries",
        sa.Column(
            "edit_history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "generation_jobs",
        sa.Column("phase", sa.String(length=16), nullable=False, server_default="summary"),
    )


def downgrade() -> None:
    op.drop_column("generation_jobs", "phase")
    op.drop_column("session_summaries", "edit_history")
    op.drop_column("session_summaries", "confirmed_by")
    op.drop_column("session_summaries", "confirmed_at")
    op.drop_column("session_summaries", "revision")
    op.drop_column("session_summaries", "review_status")
    op.drop_column("session_summaries", "party_characters")
    op.drop_column("session_summaries", "language")

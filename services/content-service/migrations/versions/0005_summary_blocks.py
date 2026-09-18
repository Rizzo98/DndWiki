"""session summary as a narrative of scene blocks

Revision ID: 0005
Revises: 0004
Create Date: 2025-01-05

The summary stopped being a list of independent one-beat lines and became a
narrative: the compose call writes the session's story as prose in scene
blocks, and the DM reviews it by highlighting ARBITRARY portions of the text
instead of ticking whole lines. 'summary' keeps holding that narrative as
plain text (it is what the review works on and what the session page renders);
'summary_blocks' holds the same story split by place
([{"location": ..., "text": ...}]) so the session page can label the portions
with the location they happen in - the reading the old "Where this session
happens" panel used to show.

Rows written before this revision keep 'summary_blocks' = [] and are rendered
from 'summary' alone, so nothing has to be regenerated for the page to work.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_summaries",
        sa.Column(
            "summary_blocks", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )


def downgrade() -> None:
    op.drop_column("session_summaries", "summary_blocks")

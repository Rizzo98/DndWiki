"""beats the session recorded twice, as a flag on the draft

Revision ID: 0006
Revises: 0005
Create Date: 2025-01-06

The transcript is cut into fixed-size pieces to be read and the pieces overlap,
so a scene that falls on a cut is read from both sides and described twice in
different words. The merger de-duplicates on identical text and cannot see that
the two lines are one moment, and the composer - which sees both - writes them as
two people. Measured twice: neither a rule in its prompt nor the span each beat
came from stops it.

So the pipeline stops trying to hide the problem and reports it instead.
'session_summaries.conflicts' holds the pairs it found
([{score, first: {text, from, to}, second: {...}}], app/conflicts.py) and the
session page shows them next to the narrative. It is a FLAG: nothing to answer,
nothing to dismiss, and an empty list - the normal case - renders nothing.

Rows written before this revision carry '[]' and behave exactly as before.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_summaries",
        sa.Column(
            "conflicts", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )


def downgrade() -> None:
    op.drop_column("session_summaries", "conflicts")

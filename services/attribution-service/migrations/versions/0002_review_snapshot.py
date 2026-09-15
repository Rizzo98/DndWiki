"""review snapshot: rebuild the review without re-running the LLM pass

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-04

The review API needs a Propagator to answer "what would this answer change?".
Rebuilding it from scratch would mean re-running the identity-evidence pass (an
LLM call per chunk) on every page load, so the pass stores its belief on
session_belief_stats. It is a CACHE: utterance_evidence remains the log the
belief is derived from, and a snapshot whose engine_version does not match the
running engine is discarded and recomputed.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_belief_stats",
        sa.Column("belief", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "session_belief_stats", sa.Column("engine_version", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "session_belief_stats", sa.Column("stop_reason", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("session_belief_stats", "stop_reason")
    op.drop_column("session_belief_stats", "engine_version")
    op.drop_column("session_belief_stats", "belief")

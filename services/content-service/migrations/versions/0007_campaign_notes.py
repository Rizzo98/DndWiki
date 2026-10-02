"""campaign notes + the note plan: the DM toolkit's 'plan' tool

Revision ID: 0007
Revises: 0006
Create Date: 2025-01-07

The session pipeline turns what the TABLE said into wiki pages. The DM also
arrives with material that was never said out loud - the world being set up
before the first session, the ground being prepared between two of them - and
until now there was nowhere to put it.

'campaign_notes' holds that free text: a title, a body, and the DM's own
bookkeeping status. It is deliberately schema-less prose, because what the DM
writes there is not yet a wiki page: it is a note to themselves.

'note_plans' holds the PROPOSED wiki changes a selection of those notes was
turned into. It mirrors 'wiki_change_sets' (revision 0004) - the same review
layer, the same changes/relations/skipped payloads, the same before/after per
change - with two differences that come from having no session behind it:

- the row IS the job. A session's progress lives in session-service, so its
  generation runs are recorded in 'generation_jobs'; nothing carries a note
  plan's progress, so 'status' does (generating -> draft -> applying ->
  applied, or failed), which is also what the plan page polls.
- it is keyed by CAMPAIGN, not by session (unique campaign_id): a campaign has
  one plan under construction, which is what makes regenerating from a fresh
  selection an UPDATE of the proposal rather than a second, competing one.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_campaign_notes_campaign_id", "campaign_notes", ["campaign_id"])

    op.create_table(
        "note_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="generating"),
        sa.Column("note_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("llm_provider", sa.String(length=64), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
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
    op.create_index("ix_note_plans_campaign_id", "note_plans", ["campaign_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_note_plans_campaign_id", table_name="note_plans")
    op.drop_table("note_plans")
    op.drop_index("ix_campaign_notes_campaign_id", table_name="campaign_notes")
    op.drop_table("campaign_notes")

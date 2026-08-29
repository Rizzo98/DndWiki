"""campaign_members: add character_description

Members carry the DM-curated physical description of the character the
player plays. It is nullable (existing rows and invite-joined members have
no description until the DM fills it in) and feeds the refiner-service LLM
contextual pass, which uses names + descriptions to correct transcription
errors and attribute speakers.

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-03

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
    op.add_column(
        "campaign_members",
        sa.Column("character_description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaign_members", "character_description")

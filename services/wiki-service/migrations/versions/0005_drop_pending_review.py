"""drop the pending_review page status

Revision ID: 0005
Revises: 0003
Create Date: 2025-01-02

Pages are never 'pending review' anymore: the pipeline writes its pages only
after the DM confirmed the proposed changes (as published), so the 55 legacy
rows of the old draft-approval flow become plain drafts the DM can publish,
edit or archive by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENDING_REVIEW = "pending_review"
DRAFT = "draft"


def upgrade() -> None:
    op.execute(sa.text(f"UPDATE wiki_pages SET status = '{DRAFT}' WHERE status = '{PENDING_REVIEW}'"))


def downgrade() -> None:
    # Irreversible on purpose: the old status carried no information the DM
    # acted on (nothing was ever waiting for one review per page). Drafts stay
    # drafts.
    pass

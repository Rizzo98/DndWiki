"""narrow wiki page kinds to character/location/faction/item/quest

Removes the event / session_note / article page types:

- session recaps live on the dedicated session page (content-service summary);
- events will surface on the timeline view in a later iteration, not as
  standalone wiki pages.

The rows are hard-deleted (the one sanctioned exception to "pages are never
hard-deleted", same as the debug reset): page_versions and page_relations
cascade, timeline_events.page_id becomes NULL via SET NULL.

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-01

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEEP = ("character", "location", "faction", "item", "quest")


def upgrade() -> None:
    keep = ", ".join("'" + k + "'" for k in KEEP)
    op.execute(
        "DELETE FROM wiki_pages WHERE kind NOT IN (" + keep + ")"
    )


def downgrade() -> None:
    # Data deleted by the upgrade cannot be restored; the removed kinds are
    # no longer part of the schema contract either. Nothing to do.
    pass

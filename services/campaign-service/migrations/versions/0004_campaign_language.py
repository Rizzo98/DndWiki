"""campaigns: add language

The DM chooses the language in which all sessions will be while creating the
campaign (required in the API, restricted to SUPPORTED_LANGUAGES). The column
is NOT NULL; pre-existing campaigns default to English ("en").

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("language", sa.String(35), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "language")

"""speaker_assignments: member_id (assign by campaign member, userless allowed)

A speaker can now be assigned to a campaign member that has no linked user
account. member_id is the surrogate campaign-member id (campaign-service);
user_id stays for user-linked members and drives voiceprint enrollment.

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("speaker_assignments", sa.Column("member_id", sa.Uuid(), nullable=True))
    op.create_index("ix_speaker_assignments_member_id", "speaker_assignments", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_speaker_assignments_member_id", table_name="speaker_assignments")
    op.drop_column("speaker_assignments", "member_id")

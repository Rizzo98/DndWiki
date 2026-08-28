"""initial schema: campaigns, campaign_members, invites

Revision ID: 0001
Revises:
Create Date: 2025-01-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("dm_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "settings",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_campaigns_slug"),
    )
    op.create_index("ix_campaigns_slug", "campaigns", ["slug"])
    op.create_index("ix_campaigns_dm_user_id", "campaigns", ["dm_user_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "campaign_members",
        sa.Column(
            "campaign_id",
            sa.Uuid(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="player"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_campaign_members_user_id", "campaign_members", ["user_id"])

    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Uuid(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="player"),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token", name="uq_invites_token"),
    )
    op.create_index("ix_invites_campaign_id", "invites", ["campaign_id"])
    op.create_index("ix_invites_token", "invites", ["token"])


def downgrade() -> None:
    op.drop_index("ix_invites_token", table_name="invites")
    op.drop_index("ix_invites_campaign_id", table_name="invites")
    op.drop_table("invites")
    op.drop_index("ix_campaign_members_user_id", table_name="campaign_members")
    op.drop_table("campaign_members")
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_index("ix_campaigns_dm_user_id", table_name="campaigns")
    op.drop_index("ix_campaigns_slug", table_name="campaigns")
    op.drop_table("campaigns")

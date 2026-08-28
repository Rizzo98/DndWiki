"""initial schema: users, voice_profiles

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
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("keycloak_sub", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("avatar_uri", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("keycloak_sub", name="uq_users_keycloak_sub"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_keycloak_sub", "users", ["keycloak_sub"])

    op.create_table(
        "voice_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("qdrant_point", sa.String(length=64), nullable=False),
        sa.Column("sample_uri", sa.String(length=512), nullable=False),
        sa.Column(
            "embedding_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "campaign_id", name="uq_voice_profiles_user_campaign"),
    )
    op.create_index("ix_voice_profiles_user_id", "voice_profiles", ["user_id"])
    op.create_index("ix_voice_profiles_campaign_id", "voice_profiles", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_voice_profiles_campaign_id", table_name="voice_profiles")
    op.drop_index("ix_voice_profiles_user_id", table_name="voice_profiles")
    op.drop_table("voice_profiles")
    op.drop_index("ix_users_keycloak_sub", table_name="users")
    op.drop_table("users")

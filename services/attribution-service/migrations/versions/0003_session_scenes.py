"""session scenes: where the session happens and who is there

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-05

The scene reading (attribution-model S12.6) produces the stretches of a session,
the place of each, and who the record puts there - and, most importantly, who it
puts SOMEWHERE ELSE, which the engine applies as a per-moment exclusion. Keeping
it only in memory meant nothing could explain why a member was excluded from a
moment, and the DM - the only one able to correct a reading of their own table -
had nothing to look at.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_scenes",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("start_ordinal", sa.Integer(), nullable=False),
        sa.Column("end_ordinal", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("end_sec", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("first_ref", sa.String(length=32), nullable=True),
        sa.Column("last_ref", sa.String(length=32), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=False, server_default="unclear"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("present_names", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("absent_names", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("npcs", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("present_member_ids", ARRAY(sa.Uuid()), nullable=False, server_default="{}"),
        sa.Column("absent_member_ids", ARRAY(sa.Uuid()), nullable=False, server_default="{}"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_id", "index"),
    )
    op.create_index("ix_session_scenes_campaign_id", "session_scenes", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_session_scenes_campaign_id", table_name="session_scenes")
    op.drop_table("session_scenes")

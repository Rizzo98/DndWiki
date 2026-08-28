"""initial schema: sessions, session_recordings, speaker_assignments

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
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("session_no", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column("raw_audio_uri", sa.Text(), nullable=True),
        sa.Column("transcript_uri", sa.Text(), nullable=True),
        sa.Column("diarization_uri", sa.Text(), nullable=True),
        sa.Column("duration_sec", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sessions_campaign_id", "sessions", ["campaign_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])

    op.create_table(
        "session_recordings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("file_uri", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_session_recordings_session_id", "session_recordings", ["session_id"])

    op.create_table(
        "speaker_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("speaker_label", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "speaker_label", name="uq_speaker_assignment_session_label"),
    )
    op.create_index("ix_speaker_assignments_session_id", "speaker_assignments", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_speaker_assignments_session_id", table_name="speaker_assignments")
    op.drop_table("speaker_assignments")
    op.drop_index("ix_session_recordings_session_id", table_name="session_recordings")
    op.drop_table("session_recordings")
    op.drop_index("ix_sessions_status", table_name="sessions")
    op.drop_index("ix_sessions_campaign_id", table_name="sessions")
    op.drop_table("sessions")

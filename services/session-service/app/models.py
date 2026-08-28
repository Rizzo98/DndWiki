"""SQLAlchemy models for the dnd_sessions database (database-per-service).

Mirrors docs/data-model.md:

- sessions            - one row per recorded game session, status = pipeline state
- session_recordings  - uploads (sha256, size, mime); one row per upload attempt
- speaker_assignments - diarized label -> user mapping (pending | auto | confirmed)
"""

import uuid
from datetime import datetime
from decimal import Decimal

from dnd_common.db import Base
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class Session(Base):
    """A recorded D&D session and its pipeline state."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    session_no: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # uploaded|recorded|transcribing|transcribed|identifying_speakers|
    # speakers_identified|speaker_pending|generating_wiki|content_ready|
    # reviewed|published|failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploaded", server_default="uploaded", index=True
    )
    raw_audio_uri: Mapped[str | None] = mapped_column(Text)
    transcript_uri: Mapped[str | None] = mapped_column(Text)
    diarization_uri: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionRecording(Base):
    """A raw audio upload for a session (MinIO recordings bucket)."""

    __tablename__ = "session_recordings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    file_uri: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpeakerAssignment(Base):
    """Diarized label -> user mapping per session (filled by speaker-service)."""

    __tablename__ = "speaker_assignments"
    __table_args__ = (
        UniqueConstraint("session_id", "speaker_label", name="uq_speaker_assignment_session_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    speaker_label: Mapped[str] = mapped_column(String(64), nullable=False)
    # The campaign member this voice belongs to (may lack a user account).
    # user_id mirrors member.user_id when the member is user-linked and is
    # what speaker-service keys voiceprint enrollment/matching on.
    member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    # pending | auto | confirmed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

"""SQLAlchemy models for the dnd_users database (database-per-service).

Mirrors docs/data-model.md:

- users          - one row per platform user; keycloak_sub is the identity
                   (the Keycloak subject). Rows are lazy-provisioned on first
                   access from the JWT claims (no separate signup endpoint).
- voice_profiles - one row per (user, campaign) voiceprint; qdrant_point is
                   the id of the embedding in Qdrant 'voiceprints', sample_uri
                   is the MinIO clip used to enroll (bucket/key pair).
"""

import uuid
from datetime import datetime

from dnd_common.db import Base
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class User(Base):
    """A platform user (identity lives in Keycloak; this is the local profile)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    keycloak_sub: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # MinIO "bucket/key" of the avatar (wiki-assets/avatars/<user_id>.<ext>)
    avatar_uri: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoiceProfile(Base):
    """An enrolled voiceprint: the embedding lives in Qdrant, this row is the
    pointer (user, campaign, point id, source sample).

    Voiceprints are per campaign - the same person can be "Gandalf" in one
    campaign and "Dumbledore" in another. Re-enrolling the same (user, campaign)
    replaces the existing row (unique constraint) and the old Qdrant point.
    """

    __tablename__ = "voice_profiles"
    __table_args__ = (UniqueConstraint("user_id", "campaign_id", name="uq_voice_profiles_user_campaign"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # id of the embedding in Qdrant 'voiceprints' (a uuid stored as text)
    qdrant_point: Mapped[str] = mapped_column(String(64), nullable=False)
    # MinIO "bucket/key" of the enrollment clip
    sample_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    embedding_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

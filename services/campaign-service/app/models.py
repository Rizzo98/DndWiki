"""SQLAlchemy models for the dnd_campaigns database (database-per-service).

Mirrors docs/data-model.md:

- campaigns        - one row per campaign; dm_user_id is the single source of
                     truth for the DM (exactly one member row carries role 'dm')
- campaign_members - membership rows; role is 'dm' | 'player'
- invites          - email/token invites (DM creates, players accept by token)
"""

import uuid
from datetime import datetime

from dnd_common.db import Base
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Campaign(Base):
    """A D&D campaign and its mutable metadata."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    dm_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # active | archived
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active", index=True
    )
    # e.g. default page visibility; JSONB on Postgres, JSON in tests (SQLite)
    settings: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignMember(Base):
    """A member of a campaign: a player at the table, optionally linked to a user account.

    A member is identified by its own surrogate id. user_id is the optional
    link to a platform user (the Keycloak subject); unlinked members (players
    without an account) are fully supported, with player_name and
    character_name as the DM-curated display data. At most one member per
    campaign may link a given user (unique (campaign_id, user_id), NULLs
    exempt).
    """

    __tablename__ = "campaign_members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # dm | player
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="player", server_default="player"
    )
    player_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    character_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_campaign_members_campaign_user"),
    )


class Invite(Base):
    """A one-time join token for a campaign (created by the DM, accepted by a user)."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="player", server_default="player"
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

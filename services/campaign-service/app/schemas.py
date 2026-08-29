"""Pydantic request/response models for the campaign-service API."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
ROLE_PATTERN = "^(dm|player)$"


class CampaignMemberCreate(BaseModel):
    """One player to add when a campaign is created.

    player_name and character_name are the DM-curated display data;
    character_description is the physical description of the character (used
    by the refiner LLM pass to recognize who is speaking); user_id is the
    optional link to an existing platform user (Keycloak subject).
    """

    player_name: str = Field(min_length=1, max_length=255)
    character_name: str = Field(min_length=1, max_length=255)
    character_description: str = Field(min_length=1, max_length=10000)
    user_id: UUID | None = None


class CampaignCreate(BaseModel):
    """Payload for POST /api/campaigns; the caller becomes the DM.

    members is REQUIRED (at least one player at the table): the DM adds the
    roster while creating the campaign. Each member carries player name,
    character name, the character's physical description (used by the
    refiner LLM pass) and an optional link to an existing platform user.
    The DM's own member row is created automatically.
    """

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=2000)
    settings: dict[str, Any] = Field(default_factory=dict)
    members: list[CampaignMemberCreate] = Field(min_length=1)


class CampaignUpdate(BaseModel):
    """Payload for PATCH /api/campaigns/{id} (DM edits)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=2000)
    settings: dict[str, Any] | None = None


class CampaignOut(BaseModel):
    """Public campaign representation.

    my_role is filled by the API layer with the calling user's role in the
    campaign ("dm" | "player"); it lets the UI pick the DM console vs the
    player library without an extra membership call.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    description: str | None
    dm_user_id: UUID
    status: str
    settings: dict[str, Any]
    created_at: datetime
    my_role: str | None = None


class CampaignMemberOut(BaseModel):
    """A membership row as exposed to the UI."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    user_id: UUID | None
    role: str
    player_name: str
    character_name: str
    character_description: str | None = None
    joined_at: datetime


class MemberAdd(BaseModel):
    """Payload for POST /api/campaigns/{id}/members (DM adds a player).

    A member is a player at the table: player_name and character_name are
    the DM-curated display data. user_id is the optional link to an
    existing platform user (Keycloak subject) - unlinked members need no
    account. The role is always "player" (the DM row is created when the
    campaign is created; co-DM support is a later enhancement).
    """

    player_name: str = Field(min_length=1, max_length=255)
    character_name: str = Field(min_length=1, max_length=255)
    character_description: str = Field(min_length=1, max_length=10000)
    user_id: UUID | None = None


class MemberUpdate(BaseModel):
    """Payload for PATCH /api/campaigns/{id}/members/{member_id} (DM edits).

    player_name/character_name rename the DM-curated display data; user_id
    links the member to an existing user; unlink_user detaches a link.
    Sending both user_id and unlink_user=True is rejected with 400.
    """

    player_name: str | None = Field(default=None, min_length=1, max_length=255)
    character_name: str | None = Field(default=None, min_length=1, max_length=255)
    character_description: str | None = Field(default=None, min_length=1, max_length=10000)
    user_id: UUID | None = None
    unlink_user: bool = False


class InviteCreate(BaseModel):
    """Payload for POST /api/campaigns/{id}/invites (DM invites by email)."""

    email: str | None = Field(default=None, max_length=320)
    role: str = Field(default="player", pattern="^player$")


class InviteOut(BaseModel):
    """An invite as returned to the DM (includes the shareable token)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    email: str | None
    role: str
    token: str
    expires_at: datetime | None
    used_at: datetime | None
    created_at: datetime


class MembershipOut(BaseModel):
    """Response for GET /internal/membership (service-to-service)."""

    campaign_id: UUID
    user_id: UUID
    role: str | None = None


class CampaignInternalOut(BaseModel):
    """Minimal campaign record for internal consumers (service-to-service).

    content-service uses dm_user_id to tell the DM's speaker (the narrator)
    apart from the players' characters during wiki generation.
    """

    id: UUID
    name: str
    dm_user_id: UUID
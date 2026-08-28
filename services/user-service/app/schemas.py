"""Pydantic request/response models for the user-service API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProfileUpdate(BaseModel):
    """Payload for PATCH /api/users/me (profile edits).

    email is deliberately not editable here: Keycloak is the source of truth
    for identity; this service only mirrors it.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=128)


class UserOut(BaseModel):
    """A user profile as exposed to the API.

    email is only populated for the caller's own profile (/api/users/me);
    other users see a public profile without it. avatar_url is a short-lived
    presigned MinIO URL filled in by the API layer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None = None
    display_name: str
    avatar_uri: str | None = None
    avatar_url: str | None = None
    created_at: datetime


class VoiceProfileOut(BaseModel):
    """A voiceprint enrollment row as exposed to the API.

    sample_url is a short-lived presigned MinIO URL for the enrollment clip,
    filled in by the API layer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    campaign_id: UUID
    sample_uri: str
    sample_url: str | None = None
    embedding_version: int
    created_at: datetime


# ------------------------------------------------------------- internal


class InternalUserOut(BaseModel):
    """User profile as exposed to other services (service-token endpoints)."""

    display_name: str
    email: str | None = None
    avatar_uri: str | None = None


class InternalUsersOut(BaseModel):
    """GET /internal/users?ids=... -> {"users": {user_id: {...}}}."""

    users: dict[str, InternalUserOut]


class InternalVoiceProfileOut(BaseModel):
    """Enrollment status for a campaign (who has a voiceprint)."""

    user_id: UUID
    embedding_version: int
    sample_uri: str
    created_at: datetime

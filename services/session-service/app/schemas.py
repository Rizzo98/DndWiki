"""Pydantic request/response models for the session-service API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.status import SessionStatus


class SessionCreate(BaseModel):
    """Payload for POST /api/sessions."""

    campaign_id: UUID
    title: str | None = Field(default=None, max_length=255)
    session_no: int | None = Field(default=None, ge=1)


class SessionUpdate(BaseModel):
    """Payload for PATCH /api/sessions/{id} (DM edits)."""

    title: str | None = Field(default=None, max_length=255)
    session_no: int | None = Field(default=None, ge=1)


class SessionOut(BaseModel):
    """Public session representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    title: str | None
    session_no: int | None
    recorded_at: datetime | None
    status: SessionStatus
    raw_audio_uri: str | None
    transcript_uri: str | None
    diarization_uri: str | None
    duration_sec: float | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionOut):
    """GET /api/sessions/{id} — adds short-lived presigned media URLs."""

    raw_audio_url: str | None = None
    transcript_url: str | None = None
    diarization_url: str | None = None


class StatusUpdate(BaseModel):
    """Internal worker payload: move a session to a new pipeline state."""

    status: SessionStatus
    error: str | None = None


class ArtifactsUpdate(BaseModel):
    """Internal worker payload: attach transcript artifacts produced by workers."""

    transcript_uri: str | None = None
    diarization_uri: str | None = None
    duration_sec: float | None = Field(default=None, ge=0)


class SpeakerAssignmentIn(BaseModel):
    """One diarized-label -> user mapping written by speaker-service."""

    speaker_label: str = Field(min_length=1, max_length=64)
    user_id: UUID | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: str = Field(default="pending", pattern="^(pending|auto|confirmed)$")


class SpeakerAssignmentOut(BaseModel):
    """Speaker assignment as exposed to the UI."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    speaker_label: str
    member_id: UUID | None
    user_id: UUID | None
    confidence: float | None
    status: str
    assigned_by: UUID | None
    created_at: datetime
    updated_at: datetime


class SpeakerAssignRequest(BaseModel):
    """DM names a previously-unknown speaker by campaign member id.

    Members may have no linked user account (userless players); the
    assignment stores member_id and mirrors user_id when the member is
    user-linked (what voiceprint enrollment/matching keys on).
    """

    member_id: UUID
    enrolled_voiceprint: bool = False


class InternalSpeakerOut(BaseModel):
    """Flat speaker row for internal consumers (content-service generation).

    display_name is resolved from the campaign member's player_name so
    userless speakers are labeled correctly.
    character_name lets generation prefer the CHARACTER name of party
    members over player names, per the wiki convention.
    """

    label: str
    user_id: UUID | None
    display_name: str | None
    character_name: str | None = None
    status: str

"""Business logic for session-service (re-exported for the routers)."""

from app.services.sessions import (
    assign_speaker,
    campaign_speaker_history,
    confirm_assignment,
    create_session,
    delete_session,
    get_assignment,
    get_session,
    get_session_or_404,
    list_assignments,
    list_sessions,
    transition_status,
    update_artifacts,
    update_session_meta,
    upload_recording,
    upsert_assignments,
)

__all__ = [
    "assign_speaker",
    "campaign_speaker_history",
    "confirm_assignment",
    "create_session",
    "delete_session",
    "get_assignment",
    "get_session",
    "get_session_or_404",
    "list_assignments",
    "list_sessions",
    "transition_status",
    "update_artifacts",
    "update_session_meta",
    "upload_recording",
    "upsert_assignments",
]

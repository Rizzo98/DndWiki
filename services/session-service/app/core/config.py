"""session-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_sessions"
    max_upload_mb: int = 512
    # Allowed upload mime types (phone m4a/aac/wav/opus)
    allowed_audio_mimes: tuple[str, ...] = (
        "audio/mp4",
        "audio/m4a",
        "audio/aac",
        "audio/wav",
        "audio/webm",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
    )
    # campaign-service URL (membership checks)
    campaign_service_url: str = "http://localhost:8002"
    # How long a (campaign, user) -> role lookup stays valid in-process, so the
    # UI poll loop does not hammer campaign-service on every request.
    membership_cache_ttl_sec: int = 60
    minio_recordings_bucket: str = "recordings"
    minio_transcripts_bucket: str = "transcripts"
    presigned_url_ttl_sec: int = 3600


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

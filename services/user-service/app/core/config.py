"""user-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_users"

    # campaign-service URL (membership checks before voiceprint enrollment)
    campaign_service_url: str = "http://localhost:8002"

    # --- voiceprint enrollment ---
    # ECAPA-TDNN speaker embedding (same model as speaker-service)
    voice_embedding_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    # HF Hub token for the model download (unauthenticated requests are
    # throttled and log a warning). Map HF_TOKEN in compose to this field.
    hf_token: str = ""
    embedding_dim: int = 192
    embedding_version: int = 1
    voiceprint_collection: str = "voiceprints"
    # Enrollment sample guidance is 10-30 s of speech; enforce sane bounds.
    voice_sample_min_sec: float = 2.0
    voice_sample_max_sec: float = 600.0
    max_voice_sample_mb: int = 25
    # Allowed upload mime types (phone m4a/aac/wav/opus, same as session-service)
    allowed_voice_mimes: tuple[str, ...] = (
        "audio/mp4",
        "audio/m4a",
        "audio/mpeg",
        "audio/aac",
        "audio/wav",
        "audio/webm",
        "audio/ogg",
        "audio/opus",
    )

    # --- object storage ---
    minio_voice_samples_bucket: str = "voice-samples"
    minio_avatars_bucket: str = "wiki-assets"  # avatars live under wiki-assets/avatars/
    allowed_avatar_mimes: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")
    max_avatar_mb: int = 5
    presigned_url_ttl_sec: int = 3600


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

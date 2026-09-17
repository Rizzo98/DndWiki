"""campaign-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_campaigns"
    # Invites expire this many days after creation (see invites.expires_at)
    invite_ttl_days: int = 7

    # --- object storage (campaign cover art) ---
    minio_campaign_assets_bucket: str = "campaign-assets"
    allowed_image_mimes: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")
    max_image_mb: int = 8
    presigned_url_ttl_sec: int = 3600


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

"""wiki-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_wiki"
    # campaign-service URL (membership/role checks for every wiki query)
    campaign_service_url: str = "http://localhost:8002"

    # --- object storage (character portraits) ---
    # Images live under the shared wiki-assets bucket (characters/<page_id>.<ext>)
    minio_wiki_assets_bucket: str = "wiki-assets"
    allowed_image_mimes: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")
    max_image_mb: int = 5
    presigned_url_ttl_sec: int = 3600


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

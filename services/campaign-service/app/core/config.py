"""campaign-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_campaigns"
    # Invites expire this many days after creation (see invites.expires_at)
    invite_ttl_days: int = 7


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

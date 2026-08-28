"""notification-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    service_db_name: str = "dnd_content"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

"""search-service configuration."""

from functools import lru_cache

from dnd_common.config import Settings


class ServiceSettings(Settings):
    meili_index: str = "wiki_pages"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()

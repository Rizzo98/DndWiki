"""Central configuration for all backend services.

Values come from environment variables (see root ``.env.example``).
Services subclass ``Settings`` to add their own fields.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Common configuration shared by every DnD Wiki service."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- general ---
    environment: str = "development"
    log_level: str = "info"
    service_name: str = "dnd-service"
    service_port: int = 8000

    # --- postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "dnd"
    postgres_password: str = "dnd"
    # Database name is per-service (database-per-service pattern)
    service_db_name: str = "dnd"

    # --- redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- rabbitmq ---
    rabbitmq_url: str = "amqp://dnd:dnd@localhost:5672/%2f"

    # --- minio (S3) ---
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "dndminio"
    minio_secret_key: str = "dndminio123"
    minio_secure: bool = False

    # --- qdrant ---
    qdrant_url: str = "http://localhost:6333"

    # --- meilisearch ---
    meili_url: str = "http://localhost:7700"
    meili_master_key: str = "dndsearchkey"

    # --- keycloak ---
    keycloak_url: str = "http://localhost:8080"
    keycloak_realm: str = "dnd"
    keycloak_service_client_id: str = "dnd-services"
    keycloak_service_client_secret: str = "change-me"

    # --- derived ---
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL for this service's database."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.service_db_name}"
        )

    @property
    def jwks_url(self) -> str:
        return (
            f"{self.keycloak_url}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/certs"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

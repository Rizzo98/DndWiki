"""dnd_common — shared layer for DnD Wiki backend services.

Provides:
- ``config``      pydantic-settings based configuration (env-driven)
- ``auth``        Keycloak JWT validation + role/membership dependencies
- ``events``      RabbitMQ publisher/consumer helpers (idempotent topology)
- ``db``          async SQLAlchemy engine/session factory

Every service installs this package and subclasses ``Settings`` for its own
extra configuration.
"""

__version__ = "0.1.0"

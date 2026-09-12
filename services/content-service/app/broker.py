"""RabbitMQ event publisher for content-service.

Uses a single robust connection for the process lifetime (FastAPI lifespan)
and reuses dnd_common.events for the envelope + topic topology. The summary
review endpoints publish summary.regenerate / summary.confirmed, which the
existing content.generate queue binds — those routing keys are consumed by
content.generate ONLY, so publishing them has no side effects on other
services.
"""

from __future__ import annotations

import logging

import aio_pika
from aio_pika.abc import AbstractRobustConnection
from dnd_common.events import Event, declare_topology, publish

logger = logging.getLogger(__name__)


class EventPublisher:
    """Lazily-connected publisher holding one robust AMQP connection."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: AbstractRobustConnection | None = None

    async def connect(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            return
        # connect_robust transparently reconnects on broker restarts
        self._connection = await aio_pika.connect_robust(self._url)
        async with self._connection.channel() as channel:
            await declare_topology(channel)
        logger.info("RabbitMQ connected: %s", self._url)

    async def publish(self, event: Event) -> None:
        await self.connect()
        assert self._connection is not None
        await publish(self._connection, event)

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None

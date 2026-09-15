"""RabbitMQ publisher for attribution-service.

One robust connection for the process lifetime (FastAPI lifespan / worker boot),
reusing dnd_common.events for the envelope and the topology.
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

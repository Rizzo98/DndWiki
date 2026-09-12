"""RabbitMQ helpers: idempotent topology declaration, publisher, consumer.

Topology (mirrors ``infrastructure/rabbitmq/definitions.json``):

- Exchange ``dnd.events`` (topic, durable)
- Job queues (``transcription.jobs``, ``speakers.identify``,
  ``content.generate``) get a ``*.dlq`` via dead-letter configuration
- Event queues (``search.events``, ``notification.events``) are plain
  durable queues without a DLQ, matching the reference definitions file
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message, connect_robust

logger = logging.getLogger(__name__)

EXCHANGE = "dnd.events"
MAX_RETRIES = 3

TOPOLOGY: dict[str, list[str]] = {
    # queue -> routing keys it binds
    "transcription.jobs": ["session.recorded"],
    # LLM contextual refinement: consumes transcription.completed and emits
    # transcription.refined (speaker-service then matches the refined labels)
    "transcripts.refine": ["transcription.completed"],
    # identification (transcription.refined / transcription.completed when the
    # refiner is disabled) + voiceprint enrollment on DM assignment
    # (speakers.assigned) share one worker/queue
    "speakers.identify": [
        "transcription.completed",
        "transcription.refined",
        "speakers.assigned",
    ],
    # content.generate drives the whole content pipeline: the summary phase
    # (speakers.identified / speakers.assigned -> draft summary), the DM's
    # summary review loop (summary.regenerate), the change-set proposal built
    # from the confirmed summary (summary.confirmed) and the write of the
    # change set the DM confirmed (plan.confirmed)
    "content.generate": [
        "speakers.identified",
        "speakers.assigned",
        "summary.regenerate",
        "summary.confirmed",
        "plan.confirmed",
    ],
    "search.events": ["wiki.published", "wiki.updated", "wiki.archived"],
    "notification.events": [
        "wiki.draft_ready",
        "speaker.pending",
        "session.published",
        "wiki.published",
    ],
}

# Only job queues get a dead-letter queue (definitions.json agrees).
JOB_QUEUES: frozenset[str] = frozenset(
    {
        "transcription.jobs",
        "transcripts.refine",
        "speakers.identify",
        "content.generate",
    }
)

# Long-running jobs (WhisperX transcription of full sessions, LLM content
# generation) routinely outlive RabbitMQ's default consumer_timeout (30 min in
# 3.13), which closes the channel mid-job and requeues the unacked message.
# Raise it per queue so acking is bounded by the real job duration. This value
# MUST match infrastructure/rabbitmq/definitions.json: RabbitMQ rejects a
# redeclare of an existing queue with different arguments (406
# PRECONDITION_FAILED).
JOB_CONSUMER_TIMEOUT_MS = 7_200_000  # 2 h


@dataclass
class Event:
    """Domain event envelope (see docs/event-contracts.md)."""

    type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_message(self) -> Message:
        body = {
            "schema_version": 1,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "type": self.type,
            "payload": self.payload,
        }
        return Message(
            body=json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )

    @classmethod
    def from_envelope(cls, body: dict[str, Any]) -> Event:
        """Build an Event from a received wire envelope.

        The envelope carries extra contract fields (schema_version) that are
        not part of the domain object, so unknown keys are ignored.
        """
        return cls(
            type=body["type"],
            payload=body.get("payload", {}),
            **({"event_id": body["event_id"]} if "event_id" in body else {}),
            **({"occurred_at": body["occurred_at"]} if "occurred_at" in body else {}),
        )


def _queue_arguments(queue_name: str) -> dict[str, str] | None:
    """Dead-letter args for a job queue, None for event queues.

    RabbitMQ rejects a redeclare with different arguments (406
    PRECONDITION_FAILED), so every declare of the same queue must pass the
    exact same dict — declare_topology() and consume() share this helper.
    """
    if queue_name in JOB_QUEUES:
        return {
            "x-dead-letter-exchange": EXCHANGE,
            "x-dead-letter-routing-key": f"{queue_name}.dlq",
            "x-consumer-timeout": JOB_CONSUMER_TIMEOUT_MS,
        }
    return None


async def _declare_queue(
    channel: aio_pika.abc.AbstractChannel, queue_name: str
) -> aio_pika.abc.AbstractQueue:
    """Idempotently declare one topology queue with its (dead-letter) args."""
    return await channel.declare_queue(
        queue_name,
        durable=True,
        arguments=_queue_arguments(queue_name),
    )


async def declare_topology(channel: aio_pika.abc.AbstractChannel) -> None:
    """Idempotently declare the exchange, queues and bindings."""
    exchange = await channel.declare_exchange(EXCHANGE, ExchangeType.TOPIC, durable=True)
    for queue_name, routing_keys in TOPOLOGY.items():
        if queue_name in JOB_QUEUES:
            await channel.declare_queue(f"{queue_name}.dlq", durable=True)
        queue = await _declare_queue(channel, queue_name)
        for routing_key in routing_keys:
            await queue.bind(exchange, routing_key=routing_key)
    logger.info("RabbitMQ topology declared (%s queues)", len(TOPOLOGY))


async def connect_rabbitmq(url: str) -> aio_pika.abc.AbstractRobustConnection:
    """Connect to RabbitMQ, waiting until the broker accepts connections.

    aio_pika's ``connect_robust`` defaults to ``fail_fast=True``: a worker that
    boots before RabbitMQ is ready raises on the first attempt and its whole
    process dies. ``fail_fast=False`` makes the connection factory retry
    indefinitely until the first connection succeeds.
    """
    return await connect_robust(url, fail_fast=False)


async def publish(
    connection: aio_pika.abc.AbstractConnection,
    event: Event,
) -> None:
    """Publish an event to the ``dnd.events`` topic exchange (routing key = type)."""
    async with connection.channel() as channel:
        exchange = await channel.declare_exchange(EXCHANGE, ExchangeType.TOPIC, durable=True)
        await exchange.publish(event.to_message(), routing_key=event.type)


async def consume(
    connection: aio_pika.abc.AbstractConnection,
    queue_name: str,
    handler: Callable[[Event], Awaitable[None]],
    prefetch: int = 1,
) -> None:
    """Run a blocking consumer loop for ``queue_name``.

    Handlers raise to trigger a retry: the message is republished with an
    incremented ``x-retries`` header (exponential backoff), and on the final
    attempt it is rejected without requeue so it lands on ``*.dlq``.
    """
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=prefetch)
    await declare_topology(channel)
    queue = await _declare_queue(channel, queue_name)
    exchange = await channel.declare_exchange(EXCHANGE, ExchangeType.TOPIC, durable=True)

    async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            try:
                body = json.loads(message.body)
            except Exception:
                logger.exception("Malformed message for queue %s -> DLQ", queue_name)
                raise
            try:
                await handler(Event.from_envelope(body))
            except Exception:
                logger.exception("Handler failed for queue %s", queue_name)
                retries = int(message.headers.get("x-retries", 0)) if message.headers else 0
                if retries < MAX_RETRIES:
                    await asyncio.sleep(2**retries)  # 1s, 2s, 4s backoff
                    await exchange.publish(
                        Message(
                            body=message.body,
                            content_type=message.content_type,
                            delivery_mode=DeliveryMode.PERSISTENT,
                            headers={**(message.headers or {}), "x-retries": retries + 1},
                        ),
                        routing_key=body["type"],
                    )
                    logger.warning("Retrying %s (attempt %s)", body.get("type"), retries + 1)
                else:
                    # Re-raise inside message.process(requeue=False) so the
                    # message is rejected without requeue and dead-lettered
                    # via the queue x-dead-letter-exchange.
                    logger.error("Final failure for %s -> %s.dlq", body.get("type"), queue_name)
                    raise

    await queue.consume(_on_message, no_ack=False)
    logger.info("Consuming %s (Ctrl+C to stop)", queue_name)
    await asyncio.Future()  # run forever
"""Regression tests for dnd_common.events topology declaration.

The bug: consume() declared the consumed queue WITHOUT the dead-letter
arguments that declare_topology() used, so RabbitMQ rejected the redeclare
with 406 PRECONDITION_FAILED and the worker reconnected forever.

These tests pin the guarantee: every declare of a queue carries identical
arguments, and they match infrastructure/rabbitmq/definitions.json.
"""

import asyncio
import json
from pathlib import Path

from dnd_common.events import (
    JOB_CONSUMER_TIMEOUT_MS,
    JOB_QUEUES,
    TOPOLOGY,
    _declare_queue,
    _queue_arguments,
    declare_topology,
)

DEFINITIONS_JSON = (
    Path(__file__).resolve().parents[4] / "infrastructure" / "rabbitmq" / "definitions.json"
)


class FakeQueue:
    def __init__(self, name):
        self.name = name

    async def bind(self, exchange, routing_key=None):
        pass


class FakeChannel:
    """Records every queue declaration (name, durable, arguments)."""

    def __init__(self):
        self.declared = []

    async def declare_exchange(self, name, type, durable):
        return object()

    async def declare_queue(self, name, durable=True, arguments=None):
        self.declared.append((name, durable, arguments))
        return FakeQueue(name)


def test_queue_arguments_match_definitions_json():
    # Job queues: DLX + per-queue dead-letter routing key + long-job consumer timeout
    assert _queue_arguments("transcription.jobs") == {
        "x-dead-letter-exchange": "dnd.events",
        "x-dead-letter-routing-key": "transcription.jobs.dlq",
        "x-consumer-timeout": JOB_CONSUMER_TIMEOUT_MS,
    }
    assert _queue_arguments("speakers.identify")["x-dead-letter-routing-key"] == "speakers.identify.dlq"
    assert _queue_arguments("content.generate")["x-dead-letter-routing-key"] == "content.generate.dlq"
    # Event queues: no dead-letter arguments (definitions.json declares them plain)
    assert _queue_arguments("search.events") is None
    assert _queue_arguments("notification.events") is None


def test_definitions_json_job_queue_args_match():
    """dnd_common's queue arguments must mirror definitions.json exactly —
    RabbitMQ rejects a redeclare of an existing queue whose arguments differ
    (406 PRECONDITION_FAILED)."""
    data = json.loads(DEFINITIONS_JSON.read_text(encoding="utf-8"))
    queues = {q["name"]: q for q in data["queues"]}
    for queue_name in JOB_QUEUES:
        assert queue_name in queues, f"{queue_name} missing from definitions.json"
        assert queues[queue_name]["arguments"] == _queue_arguments(queue_name), queue_name


def test_job_queue_set_covers_topology():
    for queue_name in TOPOLOGY:
        if queue_name in JOB_QUEUES:
            assert _queue_arguments(queue_name) is not None
        else:
            assert _queue_arguments(queue_name) is None


def test_declare_topology_declares_expected_arguments():
    channel = FakeChannel()
    asyncio.run(declare_topology(channel))

    declares = {name: args for name, _, args in channel.declared}
    # every topology queue declared, with the right arguments
    for queue_name in TOPOLOGY:
        assert queue_name in declares
        assert declares[queue_name] == _queue_arguments(queue_name)
    # only job queues get a *.dlq queue
    for queue_name in JOB_QUEUES:
        assert f"{queue_name}.dlq" in declares
        assert declares[f"{queue_name}.dlq"] is None
    for queue_name in set(TOPOLOGY) - JOB_QUEUES:
        assert f"{queue_name}.dlq" not in declares


def test_consume_redeclare_is_idempotent():
    """The exact failure: a second declare of the consumed queue must pass the
    SAME arguments as declare_topology, or RabbitMQ 406s."""
    channel = FakeChannel()
    asyncio.run(declare_topology(channel))
    asyncio.run(_declare_queue(channel, "transcription.jobs"))  # what consume() does

    args_seen = [
        args for name, _, args in channel.declared if name == "transcription.jobs"
    ]
    assert args_seen == [
        _queue_arguments("transcription.jobs"),
        _queue_arguments("transcription.jobs"),
    ]

    # same guarantee for the other consumed queues
    for queue_name in ("speakers.identify", "content.generate", "search.events"):
        asyncio.run(_declare_queue(channel, queue_name))
        args_seen = [args for name, _, args in channel.declared if name == queue_name]
        assert args_seen == [_queue_arguments(queue_name), _queue_arguments(queue_name)]


def test_event_envelope_roundtrip():
    from dnd_common.events import Event

    event = Event(type="session.recorded", payload={"session_id": "abc"})
    message = event.to_message()
    assert message.content_type == "application/json"
    assert message.delivery_mode.value == 2  # PERSISTENT
    import json

    body = json.loads(message.body)
    assert body["type"] == "session.recorded"
    assert body["payload"] == {"session_id": "abc"}
    assert body["schema_version"] == 1

    # wire envelope -> domain object (the consume() path)
    parsed = Event.from_envelope(body)
    assert parsed.type == "session.recorded"
    assert parsed.payload == {"session_id": "abc"}
    assert parsed.event_id == body["event_id"]
    assert parsed.occurred_at == body["occurred_at"]


def test_from_envelope_accepts_full_contract_envelope():
    """The reported bug: Event(**body) crashed on the extra schema_version
    field that every published envelope carries. from_envelope must accept
    the full wire format and ignore unknown/contract-only keys."""
    from dnd_common.events import Event

    envelope = {
        "schema_version": 1,
        "event_id": "5f0c4f86-63ee-4a47-9b63-8d21d6f8a8b1",
        "occurred_at": "2025-01-01T12:00:00+00:00",
        "type": "session.recorded",
        "payload": {"session_id": "abc", "campaign_id": "def"},
    }
    event = Event.from_envelope(envelope)
    assert event.type == "session.recorded"
    assert event.payload == {"session_id": "abc", "campaign_id": "def"}
    assert event.event_id == envelope["event_id"]
    assert event.occurred_at == envelope["occurred_at"]


def test_from_envelope_tolerates_missing_optional_fields():
    from dnd_common.events import Event

    # consumers may receive envelopes without event_id/occurred_at; defaults apply
    event = Event.from_envelope({"type": "session.recorded", "payload": {}})
    assert event.type == "session.recorded"
    assert event.payload == {}
    assert event.event_id  # generated
    assert event.occurred_at  # generated


def test_connect_rabbitmq_retries_forever(monkeypatch):
    """The reported bug: connect_robust defaults to fail_fast=True, so a worker
    booting before RabbitMQ is ready raises on the first attempt and dies.
    connect_rabbitmq must disable fail-fast so the connection factory retries
    until the broker accepts connections.
    """
    from dnd_common import events as events_module

    captured = {}

    async def fake_connect_robust(url, fail_fast=True):
        captured["url"] = url
        captured["fail_fast"] = fail_fast
        return "connection"

    monkeypatch.setattr(events_module, "connect_robust", fake_connect_robust)

    result = asyncio.run(events_module.connect_rabbitmq("amqp://dnd:dnd@rabbitmq:5672/"))
    assert result == "connection"
    assert captured == {
        "url": "amqp://dnd:dnd@rabbitmq:5672/",
        "fail_fast": False,
    }


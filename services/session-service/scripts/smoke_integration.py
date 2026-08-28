"""Integration smoke test: real Postgres + MinIO + RabbitMQ.

Run after the dev infra is up (docker compose up -d postgres rabbitmq minio minio-init)
and the schema migrated (alembic upgrade head). Uses localhost defaults from
ServiceSettings; no Keycloak/campaign-service required.

    ..\\..\\.venv\\Scripts\\python.exe scripts/smoke_integration.py

Exits non-zero on any failed assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import uuid
from pathlib import Path

# Every service ships a top-level "app" package; when several are pip-installed
# editable into one venv, "import app" is ambiguous. Pin this service's root
# first so the script always exercises session-service.
_SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

import aio_pika
import httpx
from dnd_common.db import get_engine, session_factory
from dnd_common.events import declare_topology
from fastapi import UploadFile

from app import services
from app.broker import EventPublisher
from app.core.config import get_settings
from app.status import SessionStatus
from app.storage import ObjectStorage


async def _fetch_object(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _consume_recorded_event(publisher: EventPublisher) -> dict:
    """Read one message off transcription.jobs (bound to session.recorded)."""
    connection = await aio_pika.connect_robust(get_settings().rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)  # idempotent; matches existing queue args
        queue = await channel.declare_queue(
            "transcription.jobs",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dnd.events",
                "x-dead-letter-routing-key": "transcription.jobs.dlq",
            },
        )
        message = await queue.get(timeout=15)
        if message is None:
            raise AssertionError("no session.recorded message consumed within timeout")
        await message.ack()
        return json.loads(message.body)


async def main() -> int:
    settings = get_settings()
    storage = ObjectStorage(settings)
    publisher = EventPublisher(settings.rabbitmq_url)
    await publisher.connect()
    factory = session_factory(settings)

    # clear any messages left by earlier smoke runs so queue.get below is deterministic
    async with await aio_pika.connect_robust(settings.rabbitmq_url) as purge_conn:
        purge_channel = await purge_conn.channel()
        await declare_topology(purge_channel)
        purge_queue = await purge_channel.declare_queue(
            "transcription.jobs",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "dnd.events",
                "x-dead-letter-routing-key": "transcription.jobs.dlq",
            },
        )
        await purge_queue.purge()

    try:
        async with factory() as db:
            session = await services.create_session(db, uuid.uuid4(), "Smoke Session", 1)
            sid = session.id
            print(f"[1] created session {sid} (status={session.status})")

        data = b"fake-phone-recording-" * 64
        sha = hashlib.sha256(data).hexdigest()
        async with factory() as db:
            upload = UploadFile(
                file=io.BytesIO(data),
                filename="rec.m4a",
                headers={"content-type": "audio/mp4"},
            )
            recorded = await services.upload_recording(
                db, sid, uuid.uuid4(), upload, storage, publisher, settings,
                duration_sec=123.5,
            )
            assert recorded.status == SessionStatus.RECORDED.value
            key = recorded.raw_audio_uri
            print(f"[2] uploaded {key} ({len(data)} bytes, sha256={sha[:12]}...)")

            # pull the object back from MinIO and compare
            url = await storage.presigned_get("recordings", key)
            fetched = await _fetch_object(url)
            assert fetched == data, "object bytes differ after round-trip"
            print("[3] MinIO round-trip OK (presigned GET returns identical bytes)")

            # worker updates pipeline state via the service layer
            updated = await services.transition_status(
                db, sid, SessionStatus.TRANSCRIBING
            )
            assert updated.status == SessionStatus.TRANSCRIBING.value
            print("[4] state machine transition OK (uploaded -> recorded -> transcribing)")

        event = await _consume_recorded_event(publisher)
        assert event["type"] == "session.recorded", event
        payload = event["payload"]
        assert payload["session_id"] == str(sid)
        assert payload["campaign_id"] == str(session.campaign_id)
        assert payload["audio_uri"] == key
        assert payload["duration_sec"] == 123.5
        print(f"[5] session.recorded consumed from transcription.jobs (event_id={event['event_id'][:8]}...)")

        print("SMOKE OK")
        return 0
    finally:
        await publisher.close()
        await get_engine(settings).dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

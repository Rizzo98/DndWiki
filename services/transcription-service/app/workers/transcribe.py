"""WhisperX transcription worker.

Consumes transcription.jobs (routing key session.recorded), runs the WhisperX
pipeline (VAD -> ASR -> diarization -> word alignment) on the raw recording,
stores transcript artifacts in MinIO and publishes transcription.completed.

Long recordings are split into WHISPER_CHUNK_SECONDS chunks; each chunk is
transcribed in a worker thread, and after every chunk the partial artifacts
are overwritten in MinIO (same URIs the UI polls) and a
transcription.progress event is published — results become visible before the
session completes, and every chunk stays far below the broker consumer
timeout.

Session state is driven exclusively through the session-service internal API:
    recorded -> transcribing -> transcribed (+artifacts), or -> failed on error.

Failure/retry semantics:
- transient failures before entering 'transcribing' (broker/HTTP outage)
  re-raise so dnd_common.consume retries with exponential backoff;
- pipeline failures mark the session 'failed' (recorded in dnd_sessions) and
  re-raise; the redelivered copy is then acked as a no-op (the state machine
  rejects the transition -> ConflictTransition), so a failed job is never
  re-run or DLQ-spammed.
- cancellation (channel closed by the broker, e.g. consumer timeout, or
  shutdown) is caught explicitly: the session is marked 'failed' so it is
  never left stuck in 'transcribing', then re-raised. Note that
  asyncio.to_thread cannot kill the running thread, so the WhisperX pipeline
  keeps consuming CPU/RAM in the background until it finishes on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aio_pika
from dnd_common.events import Event, connect_rabbitmq, consume, publish

from app.clients.session_service import (
    STATUS_FAILED,
    STATUS_TRANSCRIBED,
    STATUS_TRANSCRIBING,
    ConflictTransition,
    SessionServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.pipeline import (
    build_diarization,
    build_transcript,
    load_audio,
    offset_segments,
    split_audio,
    transcribe_audio,
)
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None


def _get_settings() -> ServiceSettings:
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def get_storage(settings: ServiceSettings) -> ObjectStorage:
    global _storage
    if _storage is None:
        _storage = ObjectStorage(settings)
    return _storage


def get_client(settings: ServiceSettings) -> SessionServiceClient:
    global _client
    if _client is None:
        _client = SessionServiceClient(settings)
    return _client


async def process_job(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    publisher: Callable[[Event], Awaitable[None]],
) -> None:
    """Transcribe one session end-to-end; raises on failure (retry applies)."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    audio_uri = str(payload["audio_uri"])
    duration_sec = payload.get("duration_sec")

    logger.info("Transcribing session %s (%s)", session_id, audio_uri)

    # Enter the pipeline state; a 409 means another delivery already moved the
    # session (idempotency guard) -> nothing to do, ack and skip.
    try:
        await client.update_status(session_id, STATUS_TRANSCRIBING)
    except ConflictTransition:
        logger.info("session %s already past %s; skipping", session_id, STATUS_TRANSCRIBING)
        return

    workdir = Path(settings.work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    staged: str | None = None
    try:
        suffix = Path(audio_uri).suffix or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=workdir, delete=False) as fh:
            staged = fh.name

        # audio_uri is "recordings/<session_id>/raw.<ext>"
        await storage.download_file(settings.minio_recordings_bucket, audio_uri, staged)

        # WhisperX is CPU/GPU-bound: decode + per-chunk inference run off the
        # event loop so RabbitMQ heartbeats and the health API stay responsive.
        # Long recordings are processed in fixed-length chunks, one at a time;
        # after each chunk the partial transcript is written to MinIO (same
        # URIs the UI polls) and a transcription.progress event is published,
        # so users see results before the whole session is done and a single
        # chunk stays far below any broker consumer timeout.
        audio = await asyncio.to_thread(load_audio, staged)
        chunks = split_audio(audio, settings.whisper_chunk_seconds)
        if not chunks:
            raise ValueError(f"audio for session {session_id} decoded to 0 samples")

        prefix = f"transcripts/{session_id}"
        transcript_uri = f"{prefix}/transcript.json"
        diarization_uri = f"{prefix}/diarization.json"

        language: str | None = None
        all_segments: list[dict[str, Any]] = []
        diarization: dict[str, Any] | None = None
        for index, (start, end, chunk_audio) in enumerate(chunks):
            logger.info(
                "session %s: transcribing chunk %d/%d (%.0f-%.0fs)",
                session_id,
                index + 1,
                len(chunks),
                start,
                end,
            )
            result = await asyncio.to_thread(
                transcribe_audio, chunk_audio, settings, language
            )
            language = language or result.get("language")
            all_segments.extend(offset_segments(result, start, chunk=index))

            _, diarization = await _store_artifacts(
                storage,
                settings,
                session_id,
                transcript_uri,
                diarization_uri,
                all_segments,
                language,
            )
            await _report_progress(
                client,
                publisher,
                session_id,
                campaign_id,
                audio_uri,
                transcript_uri,
                diarization_uri,
                language,
                diarization,
                index,
                len(chunks),
            )
            logger.info(
                "session %s: chunk %d/%d done (%d segments so far)",
                session_id,
                index + 1,
                len(chunks),
                len(all_segments),
            )

        # The artifacts already carry the final content (the last chunk
        # overwrote them); fix the authoritative duration and move to
        # transcribed BEFORE publishing so the next stage (speaker-service)
        # never observes a stale status.
        actual_duration = float(duration_sec) if duration_sec is not None else None
        if actual_duration is None:
            actual_duration = all_segments[-1]["end"] if all_segments else 0.0
        await client.update_artifacts(
            session_id, transcript_uri, diarization_uri, actual_duration
        )
        await client.update_status(session_id, STATUS_TRANSCRIBED)

        assert diarization is not None  # chunks is non-empty by now
        segments = diarization["segments"]
        await publisher(
            Event(
                type="transcription.completed",
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "language": language,
                    "audio_uri": audio_uri,
                    "transcript_uri": transcript_uri,
                    "diarization_uri": diarization_uri,
                    "segments": segments,
                },
            )
        )
        logger.info("session %s transcribed (%d segments)", session_id, len(segments))
    except asyncio.CancelledError:
        # The consumer task was cancelled mid-pipeline (broker closed the
        # channel — e.g. consumer timeout — or the process is shutting down).
        # The worker thread cannot be stopped, but the session must not be
        # left in 'transcribing' forever: best-effort mark it failed so the
        # redelivered copy is acked as a no-op (ConflictTransition) instead of
        # re-running. CancelledError is a BaseException, so it would otherwise
        # bypass the generic handler below and leave the state machine stuck.
        logger.warning("transcription cancelled for session %s", session_id)
        try:
            await client.update_status(session_id, STATUS_FAILED, error="cancelled")
        except Exception:
            logger.exception("could not mark session %s failed after cancellation", session_id)
        raise
    except Exception as exc:
        logger.exception("transcription failed for session %s", session_id)
        try:
            await client.update_status(session_id, STATUS_FAILED, error=str(exc)[:2000])
        except Exception:
            logger.exception("could not mark session %s failed", session_id)
        raise
    finally:
        if staged is not None:
            try:
                os.unlink(staged)
            except OSError:  # already gone / permission
                pass


async def _store_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    session_id: str,
    transcript_uri: str,
    diarization_uri: str,
    segments: list[dict[str, Any]],
    language: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write (overwrite) the current transcript/diarization artifacts to MinIO.

    Called after every chunk with the accumulated segments, so the objects at
    transcript_uri/diarization_uri always reflect the latest progress and the
    final call leaves the complete result in place.
    """
    transcript = build_transcript(
        session_id, {"language": language, "segments": segments}, settings.whisper_model
    )
    diarization = build_diarization(
        session_id, {"language": language, "segments": segments}, settings.whisper_model
    )
    await storage.put_bytes(
        settings.minio_transcripts_bucket,
        transcript_uri,
        json.dumps(transcript).encode("utf-8"),
        "application/json",
    )
    await storage.put_bytes(
        settings.minio_transcripts_bucket,
        diarization_uri,
        json.dumps(diarization).encode("utf-8"),
        "application/json",
    )
    return transcript, diarization


async def _report_progress(
    client: SessionServiceClient,
    publisher: Callable[[Event], Awaitable[None]],
    session_id: str,
    campaign_id: str,
    audio_uri: str,
    transcript_uri: str,
    diarization_uri: str,
    language: str | None,
    diarization: dict[str, Any],
    chunk_index: int,
    total_chunks: int,
) -> None:
    """Best-effort per-chunk reporting: keep the session artifact pointers
    fresh and emit transcription.progress. Failures here must not fail the
    job — the final state transitions are handled by the caller.
    """
    try:
        duration_so_far = (
            diarization["segments"][-1]["end"] if diarization["segments"] else None
        )
        await client.update_artifacts(
            session_id, transcript_uri, diarization_uri, duration_so_far
        )
    except Exception:
        logger.exception("could not record progress artifacts for session %s", session_id)
    try:
        await publisher(
            Event(
                type="transcription.progress",
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "language": language,
                    "audio_uri": audio_uri,
                    "transcript_uri": transcript_uri,
                    "diarization_uri": diarization_uri,
                    "segments": diarization["segments"],
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                },
            )
        )
    except Exception:
        logger.exception("could not publish progress for session %s", session_id)


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: wire process_job with process-level singletons."""
    settings = _get_settings()
    storage = get_storage(settings)
    client = get_client(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_job(event, settings, storage, client, publisher)


async def main() -> None:
    settings = _get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(
        connection,
        "transcription.jobs",
        lambda event: handle(event, connection),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
"""Deepgram Speech-to-Text transcription worker (Nova 3, chunked uploads).

Consumes transcription.jobs (routing key session.recorded) and produces the
same artifacts and events the on-prem WhisperX worker produces, so the rest
of the pipeline (refiner-service, speaker-service, content-service, web UI)
is unaware of the backend:

    recorded -> transcribing -> transcribed
        + transcripts/<session>/transcript.json
        + transcripts/<session>/diarization.json
        -> transcription.completed (segments with speaker labels)

Long recordings are decoded locally (ffmpeg -> 16 kHz mono WAV) and sliced
into fixed-length chunks (DEEPGRAM_CHUNK_SECONDS); each chunk is sent to the
Deepgram API separately, its diarized segments are offset back into the
session timeline and merged in sequence. After every chunk the partial
artifacts are overwritten in MinIO (same URIs the UI polls) and a
transcription.progress event is published, so results become visible before
the session completes — exactly like the on-prem worker. Speaker
identification then runs locally, unchanged.

Session state is driven exclusively through the session-service internal API.
Failure/retry semantics mirror the WhisperX worker exactly:
- transient failures re-raise so dnd_common.consume retries with backoff;
- pipeline failures mark the session 'failed' and re-raise; the redelivered
  copy is acked as a no-op (ConflictTransition) so a failed job is never
  re-run or DLQ-spammed;
- cancellation (broker channel close / shutdown) marks the session 'failed'
  so it is never stuck in 'transcribing', then re-raises.

No local ML models are involved: only ffmpeg for decode (no torch/whisperx/
pyannote, no GPU, no model downloads, no models volume).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import aio_pika
from dnd_common.events import Event, connect_rabbitmq, consume, publish

from app.artifacts import build_diarization, build_transcript, offset_segments
from app.audio import decode_to_wav, split_wav
from app.clients.deepgram_transcriber import DeepgramTranscriber
from app.clients.session_service import (
    STATUS_FAILED,
    STATUS_TRANSCRIBED,
    STATUS_TRANSCRIBING,
    ConflictTransition,
    SessionServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None
_transcriber: DeepgramTranscriber | None = None


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


def get_transcriber(settings: ServiceSettings) -> DeepgramTranscriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = DeepgramTranscriber(settings)
    return _transcriber


async def process_job(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    transcriber: DeepgramTranscriber,
    publisher: Callable[[Event], Awaitable[None]],
) -> None:
    """Transcribe one session end-to-end; raises on failure (retry applies)."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    audio_uri = str(payload["audio_uri"])
    duration_sec = payload.get("duration_sec")

    logger.info("Transcribing session %s (%s) via Deepgram", session_id, audio_uri)

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
    wav_path: str | None = None
    chunk_dir: str | None = None
    try:
        suffix = Path(audio_uri).suffix or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=workdir, delete=False) as fh:
            staged = fh.name

        # audio_uri is "recordings/<session_id>/raw.<ext>"
        await storage.download_file(settings.minio_recordings_bucket, audio_uri, staged)

        # Decode to 16 kHz mono PCM WAV (ffmpeg only) and slice into
        # fixed-length chunks, each well below Deepgram's per-request limits.
        # decode/split are CPU-bound but light; run them off the event loop.
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=workdir, delete=False) as fh:
            wav_path = fh.name
        await asyncio.to_thread(decode_to_wav, staged, wav_path)
        chunk_dir = str(Path(workdir) / f"chunks-{uuid.uuid4().hex}")
        max_bytes = settings.deepgram_max_upload_mb * 1024 * 1024
        chunks = await asyncio.to_thread(
            split_wav, wav_path, settings.deepgram_chunk_seconds, max_bytes, chunk_dir
        )
        if not chunks:
            raise ValueError(f"audio for session {session_id} decoded to 0 samples")

        prefix = f"transcripts/{session_id}"
        transcript_uri = f"{prefix}/transcript.json"
        diarization_uri = f"{prefix}/diarization.json"

        language = settings.deepgram_language.strip() or None
        all_segments: list[dict] = []
        diarization: dict | None = None
        for index, chunk in enumerate(chunks):
            logger.info(
                "session %s: transcribing chunk %d/%d (%.0f-%.0fs)",
                session_id,
                index + 1,
                len(chunks),
                chunk.start_sec,
                chunk.end_sec,
            )
            # The HTTP call is async, so the event loop stays responsive
            # (RabbitMQ heartbeats, health API) between API round-trips.
            result = await transcriber.transcribe(chunk.path, language=language)
            language = language or result.language
            all_segments.extend(offset_segments(result.segments, chunk.start_sec, chunk=index))

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
        # transcribed BEFORE publishing so the next stage (refiner /
        # speaker-service) never observes a stale status.
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
        # The session must not be left in 'transcribing' forever: best-effort
        # mark it failed so the redelivered copy is acked as a no-op
        # (ConflictTransition) instead of re-running.
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
        for path in (staged, wav_path):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:  # already gone / permission
                    pass
        if chunk_dir is not None:
            shutil.rmtree(chunk_dir, ignore_errors=True)


async def _store_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    session_id: str,
    transcript_uri: str,
    diarization_uri: str,
    segments: list[dict],
    language: str | None,
) -> tuple[dict, dict]:
    """Write (overwrite) the current transcript/diarization artifacts to MinIO.

    Called after every chunk with the accumulated segments, so the objects at
    transcript_uri/diarization_uri always reflect the latest progress and the
    final call leaves the complete result in place.
    """
    transcript = build_transcript(
        session_id, language, settings.deepgram_transcription_model, segments
    )
    diarization = build_diarization(
        session_id, language, settings.deepgram_transcription_model, segments
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
    diarization: dict,
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
    transcriber = get_transcriber(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_job(event, settings, storage, client, transcriber, publisher)


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

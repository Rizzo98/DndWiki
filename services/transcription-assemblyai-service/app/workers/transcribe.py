"""AssemblyAI Speech-to-Text transcription worker (Universal-3.5 Pro, chunked).

Consumes transcription.jobs (routing key session.recorded) and produces the
same artifacts and events the on-prem WhisperX worker produces, so the rest
of the pipeline (refiner-service, speaker-service, content-service, web UI)
is unaware of the backend:

    recorded -> transcribing -> transcribed
        + transcripts/<session>/transcript.json
        + transcripts/<session>/diarization.json
        -> transcription.completed (segments with speaker labels)

Long recordings are decoded locally (ffmpeg -> 16 kHz mono WAV) and sliced
into fixed-length chunks (ASSEMBLYAI_CHUNK_SECONDS); each chunk WAV is
uploaded to AssemblyAI, a transcript job is submitted (Universal-3.5 Pro,
speaker diarization) and polled until it completes. The diarized segments
are offset back into the session timeline and merged in sequence. After
every chunk the partial artifacts are overwritten in MinIO (same URIs the
UI polls) and a transcription.progress event is published, so results
become visible before the session completes — exactly like the on-prem
worker. Speaker identification then runs locally, unchanged.

Per-campaign parameters are read from campaign-service at job time
(best-effort, same policy as the refiner's cast fetch):
- language: the campaign's language (campaign.language) -> language_code;
- speakers_expected: the campaign's member count (dm + players at the table);
- prompt: contextual prompt (campaign name/description + built-in D&D
  domain description) -> prompt;
- keyterms: the roster's character/player names -> keyterms_prompt.
When campaign-service is unreachable the job still runs with AssemblyAI
auto-detection and the built-in prompt.

Session state is driven exclusively through the session-service internal API.
Failure/retry semantics mirror the other workers exactly:
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
from typing import Any

import aio_pika
from dnd_common.events import Event, connect_rabbitmq, consume, publish

from app.artifacts import build_diarization, build_transcript, offset_segments
from app.audio import decode_to_wav, split_wav
from app.clients.assemblyai_transcriber import AssemblyAITranscriber
from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import (
    STATUS_FAILED,
    STATUS_TRANSCRIBED,
    STATUS_TRANSCRIBING,
    ConflictTransition,
    SessionServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.prompts import build_keyterms, build_prompt
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None
_campaign_client: CampaignServiceClient | None = None
_transcriber: AssemblyAITranscriber | None = None


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


def get_campaign_client(settings: ServiceSettings) -> CampaignServiceClient:
    global _campaign_client
    if _campaign_client is None:
        _campaign_client = CampaignServiceClient(settings)
    return _campaign_client


def get_transcriber(settings: ServiceSettings) -> AssemblyAITranscriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = AssemblyAITranscriber(settings)
    return _transcriber


# AssemblyAI treats speakers_expected as a hard boundary on the number of
# speaker labels; keep the value in a sane range (a D&D table has a handful
# of people) and log when the campaign roster would exceed it.
MAX_EXPECTED_SPEAKERS = 20


async def _campaign_context(
    campaign_client: CampaignServiceClient | None,
    settings: ServiceSettings,
    campaign_id: str,
) -> tuple[str | None, int | None, str | None, list[str]]:
    """Best-effort campaign context: (language, speakers_expected, prompt, keyterms).

    A campaign-service outage or a deleted campaign never fails the job: the
    worker falls back to the ASSEMBLYAI_LANGUAGE env override (or AssemblyAI
    auto-detection), no speaker hint and the built-in prompt.
    """
    language = settings.assemblyai_language.strip() or None
    speakers_expected: int | None = None
    prompt: str | None = None
    keyterms: list[str] = []
    if campaign_client is None or not campaign_id:
        return language, speakers_expected, prompt, keyterms

    campaign: dict[str, Any] | None = None
    members: list[dict[str, Any]] | None = None
    try:
        campaign = await campaign_client.get_campaign(campaign_id)
    except Exception:  # campaign context is best-effort
        logger.warning(
            "could not fetch campaign %s for transcription context; using defaults",
            campaign_id,
            exc_info=True,
        )
    try:
        members = await campaign_client.list_members(campaign_id)
    except Exception:  # campaign context is best-effort
        logger.warning(
            "could not fetch roster for campaign %s; transcribing without a speaker count",
            campaign_id,
            exc_info=True,
        )

    if campaign is not None:
        # The campaign language (BCP-47-ish code, e.g. "it") is the primary
        # source; an explicit ASSEMBLYAI_LANGUAGE override wins over it.
        campaign_language = (campaign.get("language") or "").strip()
        if campaign_language and not language:
            language = campaign_language
        prompt = build_prompt(settings, campaign)
    if members is not None and members:
        count = len(members)
        if count > MAX_EXPECTED_SPEAKERS:
            logger.warning(
                "campaign %s has %d members; capping speakers_expected at %d",
                campaign_id,
                count,
                MAX_EXPECTED_SPEAKERS,
            )
            count = MAX_EXPECTED_SPEAKERS
        if count >= 2:
            speakers_expected = count
        if prompt is None:
            prompt = build_prompt(settings, campaign)
        keyterms = build_keyterms(settings, members)
    elif prompt is None:
        prompt = build_prompt(settings, None)
    if prompt and not prompt.strip():
        prompt = None
    return language, speakers_expected, prompt, keyterms


async def process_job(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    transcriber: AssemblyAITranscriber,
    publisher: Callable[[Event], Awaitable[None]],
    campaign_client: CampaignServiceClient | None = None,
) -> None:
    """Transcribe one session end-to-end; raises on failure (retry applies)."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    audio_uri = str(payload["audio_uri"])
    duration_sec = payload.get("duration_sec")

    logger.info("Transcribing session %s (%s) via AssemblyAI", session_id, audio_uri)

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
        # fixed-length chunks, each well below AssemblyAI's limits. decode/
        # split are CPU-bound but light; run them off the event loop.
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=workdir, delete=False) as fh:
            wav_path = fh.name
        await asyncio.to_thread(decode_to_wav, staged, wav_path)
        chunk_dir = str(Path(workdir) / f"chunks-{uuid.uuid4().hex}")
        max_bytes = settings.assemblyai_max_upload_mb * 1024 * 1024
        chunks = await asyncio.to_thread(
            split_wav, wav_path, settings.assemblyai_chunk_seconds, max_bytes, chunk_dir
        )
        if not chunks:
            raise ValueError(f"audio for session {session_id} decoded to 0 samples")

        prefix = f"transcripts/{session_id}"
        transcript_uri = f"{prefix}/transcript.json"
        diarization_uri = f"{prefix}/diarization.json"

        # Per-campaign parameters (best-effort): language, speaker count,
        # contextual prompt and keyterms.
        language, speakers_expected, prompt, keyterms = await _campaign_context(
            campaign_client, settings, campaign_id
        )
        logger.info(
            "session %s: campaign context language=%r speakers_expected=%s prompt=%s keyterms=%d",
            session_id,
            language,
            speakers_expected,
            bool(prompt),
            len(keyterms),
        )

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
            # The HTTP calls are async, so the event loop stays responsive
            # (RabbitMQ heartbeats, health API) between API round-trips.
            result = await transcriber.transcribe(
                chunk.path,
                language=language,
                speakers_expected=speakers_expected,
                prompt=prompt,
                keyterms=keyterms,
            )
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
        session_id, language, settings.assemblyai_transcription_model, segments
    )
    diarization = build_diarization(
        session_id, language, settings.assemblyai_transcription_model, segments
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
    campaign_client = get_campaign_client(settings)
    transcriber = get_transcriber(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_job(event, settings, storage, client, transcriber, publisher, campaign_client)


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

"""LLM contextual diarization worker (refiner-service).

Consumes transcripts.refine (routing key transcription.completed) and runs the
LLM contextual pass between transcription and speaker identification:

    transcribed -> refining -> refined
        + transcript.json / diarization.json rewritten with corrected text and
          consistent SPEAKER_XX labels (turn-level, timings preserved)
        -> transcription.refined (speaker-service then matches voiceprints)

The LLM is asked to fix transcription errors and speaker attribution using
context; fidelity to the exact words is secondary to narrative consistency.
Long sessions are refined in sliding windows; per-window decisions are merged,
canonicalized and mapped back onto the original segments (see app.refine).

When REFINER_ENABLED is false the worker does nothing (acks): speaker-service
identifies directly from transcription.completed, with its own embedding
re-clustering, exactly as before the refiner existed.

Failure/retry semantics mirror the other workers:
- transient failures re-raise so dnd_common.consume retries with backoff;
- pipeline failures mark the session 'failed' and re-raise; the redelivered
  copy is acked as a no-op (ConflictTransition) so a failed job is never
  re-run or DLQ-spammed;
- cancellation marks the session 'failed' so it is never stuck in 'refining'.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from dnd_common.events import Event, connect_rabbitmq, consume, publish
from dnd_common.transcript import speaker_turns

from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import (
    STATUS_FAILED,
    STATUS_REFINED,
    STATUS_REFINING,
    ConflictTransition,
    SessionServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.llm import RefinerLLM
from app.refine import (
    apply_refined,
    build_cast_block,
    build_context_blocks,
    canonicalize,
    keep_labels,
    rewrite_transcript,
    turn_view,
    turn_windows,
)
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None
_campaign_client: CampaignServiceClient | None = None
_llm: RefinerLLM | None = None


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


def get_llm(settings: ServiceSettings) -> RefinerLLM:
    global _llm
    if _llm is None:
        _llm = RefinerLLM(settings)
    return _llm


async def process_job(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    llm: RefinerLLM,
    publisher: Callable[[Event], Awaitable[None]],
    campaign_client: CampaignServiceClient | None = None,
) -> None:
    """Refine one session's transcript end-to-end; raises on failure."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    language = payload.get("language")
    # Forwarded unchanged: speaker-service needs it to download the recording
    # and embed the refined labels' voice windows for matching/enrollment.
    audio_uri = payload.get("audio_uri")
    transcript_uri = payload.get("transcript_uri") or f"transcripts/{session_id}/transcript.json"
    diarization_uri = payload.get("diarization_uri") or f"transcripts/{session_id}/diarization.json"
    event_segments = payload.get("segments") or []

    if not settings.refiner_enabled:
        logger.info(
            "refiner disabled; session %s stays on transcription.completed "
            "(speaker-service handles it)",
            session_id,
        )
        return

    # Enter the pipeline state; a 409 means another delivery already moved the
    # session (idempotency guard) -> nothing to do, ack and skip.
    try:
        await client.update_status(session_id, STATUS_REFINING)
    except ConflictTransition:
        logger.info("session %s already past %s; skipping", session_id, STATUS_REFINING)
        return

    try:
        diarization, transcript = await _read_artifacts(
            storage,
            settings,
            session_id,
            diarization_uri,
            transcript_uri,
            event_segments,
        )
        segments = diarization.get("segments") or []
        turns = speaker_turns(segments)
        logger.info(
            "session %s: refining %d turns via %s",
            session_id,
            len(turns),
            settings.effective_model,
        )

        # The campaign cast (character names + physical descriptions) gives
        # the LLM the context to correct misheard names and attribute speech
        # to the right person. Best-effort: a campaign-service outage or a
        # deleted campaign never fails refinement - the pass just runs
        # without the cast.
        cast_lines: list[str] = []
        member_count: int | None = None
        if campaign_client is not None and campaign_id:
            try:
                members = await campaign_client.list_members(campaign_id)
                cast_lines = build_cast_block(members)
                # The roster size (DM + players at the table) is the maximum
                # number of distinct speakers; mirror the speakers_expected
                # hint the transcription stage sent to the ASR backend.
                member_count = len(members) if members else None
                logger.info(
                    "session %s: %d cast member(s) injected (%d with description); "
                    "table size %s",
                    session_id,
                    len(cast_lines),
                    sum(1 for m in members if (m.get("character_description") or "").strip()),
                    member_count,
                )
            except Exception:  # cast context is best-effort
                logger.warning(
                    "could not fetch campaign cast for %s; refining without it",
                    campaign_id,
                    exc_info=True,
                )

        # REFINER_SPEAKERS decides what the model is allowed to touch. The
        # attribution engine consumes the diarizer's labels as measurements, so
        # with the engine on this must be false: a label the LLM guessed is not
        # a measurement.
        include_speakers = settings.refiner_speakers
        if not include_speakers:
            logger.info(
                "session %s: text-only refinement (REFINER_SPEAKERS=false); "
                "speaker labels are left exactly as the diarizer produced them",
                session_id,
            )

        decisions: dict[int, tuple[str, str]] = {}
        windows = turn_windows(
            len(turns), settings.refiner_window_turns, settings.refiner_window_overlap
        )
        for w_index, (start, end, fixed) in enumerate(windows):
            views = [
                turn_view(turns, segments, i, include_speakers=include_speakers)
                for i in range(start, end)
            ]
            context_blocks = (
                build_context_blocks(turns, segments, decisions) if include_speakers else []
            )
            raw = await llm.refine_window(
                views,
                context_blocks=context_blocks,
                fixed_count=fixed,
                window_index=w_index,
                total_windows=len(windows),
                cast_lines=cast_lines,
                member_count=member_count,
                include_speakers=include_speakers,
            )
            for index, item in raw.items():
                if start <= index < end and index >= start + fixed:
                    decisions[index] = item
            logger.info(
                "session %s: window %d/%d refined (%d turns)",
                session_id,
                w_index + 1,
                len(windows),
                end - start,
            )

        decisions = (
            canonicalize(turns, decisions)
            if include_speakers
            else keep_labels(turns, decisions)
        )
        refined_segments = apply_refined(segments, turns, decisions)

        refiner_meta = {
            "enabled": True,
            "provider": settings.llm_provider,
            "model": settings.effective_model,
            "prompt_version": settings.prompt_version,
            # Whether the LLM touched the speaker labels. Downstream consumers
            # (speaker-service, the attribution engine) need to know: evidence
            # built on labels an LLM guessed is not evidence.
            "speakers_refined": include_speakers,
            "cast": {
                "injected": bool(cast_lines),
                "members": len(cast_lines),
                "with_description": sum(1 for line in cast_lines if "—" in line),
                "table_size": member_count,
            },
        }
        await _rewrite_artifacts(
            storage,
            settings,
            diarization_uri,
            transcript_uri,
            diarization,
            transcript,
            refined_segments,
            refiner_meta,
        )
        await client.update_status(session_id, STATUS_REFINED)
        await publisher(
            Event(
                type="transcription.refined",
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "language": language,
                    "audio_uri": audio_uri,
                    "transcript_uri": transcript_uri,
                    "diarization_uri": diarization_uri,
                    "segments": refined_segments,
                    "refined": True,
                    "speakers_refined": include_speakers,
                    "refiner": refiner_meta,
                },
            )
        )
        logger.info("session %s refined (%d segments)", session_id, len(refined_segments))
    except asyncio.CancelledError:
        # The consumer task was cancelled mid-pipeline (broker closed the
        # channel or the process is shutting down): never leave the session
        # stuck in 'refining'. Best-effort mark it failed so the redelivered
        # copy is acked as a no-op instead of re-running.
        logger.warning("refinement cancelled for session %s", session_id)
        try:
            await client.update_status(session_id, STATUS_FAILED, error="cancelled")
        except Exception:
            logger.exception("could not mark session %s failed after cancellation", session_id)
        raise
    except Exception as exc:
        logger.exception("refinement failed for session %s", session_id)
        try:
            await client.update_status(session_id, STATUS_FAILED, error=str(exc)[:2000])
        except Exception:
            logger.exception("could not mark session %s failed", session_id)
        raise


async def _read_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    session_id: str,
    diarization_uri: str,
    transcript_uri: str,
    event_segments: list[dict],
) -> tuple[dict, dict | None]:
    """Read the transcription artifacts; fall back to the event's segments."""
    diarization: dict | None = None
    transcript: dict | None = None
    try:
        diarization = await storage.read_json(settings.minio_transcripts_bucket, diarization_uri)
    except Exception:  # noqa: BLE001 - best-effort read with a documented fallback
        logger.warning("could not read %s; using event segments", diarization_uri)
    try:
        transcript = await storage.read_json(settings.minio_transcripts_bucket, transcript_uri)
    except Exception:  # noqa: BLE001 - best-effort read with a documented fallback
        logger.warning("could not read %s; transcript will not be refined", transcript_uri)
    if diarization is None:
        diarization = {"segments": event_segments}
    return diarization, transcript


async def _rewrite_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    diarization_uri: str,
    transcript_uri: str,
    diarization: dict,
    transcript: dict | None,
    refined_segments: list[dict],
    refiner_meta: dict[str, Any],
) -> None:
    """Write the refined diarization + transcript back to MinIO (same keys)."""
    new_diarization = dict(diarization)
    new_diarization["segments"] = refined_segments
    new_diarization["refiner"] = refiner_meta
    await storage.put_json(settings.minio_transcripts_bucket, diarization_uri, new_diarization)

    if transcript is None:
        return
    new_transcript = rewrite_transcript(transcript, refined_segments)
    new_transcript["refiner"] = refiner_meta
    await storage.put_json(settings.minio_transcripts_bucket, transcript_uri, new_transcript)


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: wire process_job with process-level singletons."""
    settings = _get_settings()
    storage = get_storage(settings)
    client = get_client(settings)
    campaign_client = get_campaign_client(settings)
    llm = get_llm(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_job(event, settings, storage, client, llm, publisher, campaign_client)


async def main() -> None:
    settings = _get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(
        connection,
        "transcripts.refine",
        lambda event: handle(event, connection),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
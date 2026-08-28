"""Speaker identification + voiceprint enrollment worker.

Consumes speakers.identify (routing keys transcription.completed /
speakers.assigned) and implements both halves of speaker naming:

1. transcription.refined / transcription.completed -> identify
   Download the recording + diarized segments, then (unless the labels were
   already fixed by the refiner-service LLM contextual pass) re-cluster the raw
   diarizer labels from our own ECAPA-TDNN embeddings (overriding unreliable
   per-chunk labels; see app.relabel), rewrite transcript/diarization.json,
   and match each label to the campaign voiceprints in Qdrant. Anchors
   (per-user enrollment centroids) shortcut known speakers; otherwise a pooled
   centroid is cosine-searched and auto-assigned when the score is >=
   SPEAKER_MATCH_THRESHOLD, else left pending for the DM. The session moves
   transcribed -> identifying_speakers -> speakers_identified | speaker_pending
   (or refined -> identifying_speakers after the refiner), assignments are
   upserted via the session-service internal API, and speakers.identified /
   speaker.pending are published.

   When the refiner is enabled (REFINER_ENABLED=true), transcription.completed
   is skipped here: the refiner-service consumes it, fixes labels/text with the
   LLM, and emits transcription.refined, which is what this worker processes
   (labels matched directly, no re-clustering).

2. speakers.assigned       -> enroll
   The DM named a previously-unknown speaker: slice that label's audio out of
   the session recording, embed it and upsert it as a session-derived
   voiceprint for the assigned user in the campaign. Next session, the same
   voice auto-matches the name (the "named, old, ones" the new embedding is
   compared against).

Failure semantics mirror transcription-service: failures mark the session
'failed' and re-raise; the redelivered copy is acked as a no-op via
ConflictTransition, so a failed job is never re-run or DLQ-spammed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import aio_pika
from dnd_common.events import Event, connect_rabbitmq, consume, publish

from app.audio import slice_wav_bytes, speaker_windows
from app.clients.session_service import (
    STATUS_FAILED,
    STATUS_IDENTIFYING_SPEAKERS,
    STATUS_SPEAKER_PENDING,
    STATUS_SPEAKERS_IDENTIFIED,
    ConflictTransition,
    SessionServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.embedding import VoiceEmbedder
from app.identify import match_label
from app.qdrant import VoiceprintStore
from app.relabel import RelabelResult, centroid, relabel_segments, speaker_turns
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None
_voiceprints: VoiceprintStore | None = None
_embedder: VoiceEmbedder | None = None


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


def get_voiceprints(settings: ServiceSettings) -> VoiceprintStore:
    global _voiceprints
    if _voiceprints is None:
        _voiceprints = VoiceprintStore(settings)
    return _voiceprints


def get_embedder(settings: ServiceSettings) -> VoiceEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = VoiceEmbedder(settings)
    return _embedder


def _embed_sync(audio_path: str, start: float, end: float, embedder: VoiceEmbedder) -> tuple[list[float], float]:
    """Slice + embed inside a worker thread (torchaudio/speechbrain block)."""
    clip = slice_wav_bytes(audio_path, start, end)
    # embed_bytes is async but has no awaits; run it on its own loop in-thread.
    return asyncio.run(embedder.embed_bytes(clip, suffix=".wav"))


async def _finish(
    session_id: str,
    campaign_id: str,
    assignments: list[dict],
    client: SessionServiceClient,
    publisher: Callable[[Event], Awaitable[None]],
) -> None:
    """Move the session past identification and publish the results."""
    pending = [a["speaker_label"] for a in assignments if a["status"] == "pending"]
    if pending:
        await client.update_status(session_id, STATUS_SPEAKER_PENDING)
        await publisher(
            Event(
                type="speaker.pending",
                payload={
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "pending_labels": pending,
                },
            )
        )
    else:
        await client.update_status(session_id, STATUS_SPEAKERS_IDENTIFIED)
    await publisher(
        Event(
            type="speakers.identified",
            payload={
                "session_id": session_id,
                "campaign_id": campaign_id,
                "speakers": [
                    {
                        "label": a["speaker_label"],
                        "user_id": a["user_id"],
                        "confidence": a["confidence"],
                        "status": a["status"],
                    }
                    for a in assignments
                ],
                "pending_assignment": bool(pending),
            },
        )
    )


async def process_identified(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    publisher: Callable[[Event], Awaitable[None]],
) -> None:
    """Identify every diarized speaker of a session; raises on failure.

    For transcription.refined events (labels already fixed by the LLM
    contextual pass) each label is matched directly to the enrolled campaign
    voiceprints. For transcription.completed events the raw diarizer labels are
    re-clustered from our own embeddings first (unless relabel_enabled is
    false), then matched (enrollment anchors shortcut known speakers;
    otherwise the pooled centroid is cosine-searched). When the event carries
    no audio_uri, voice matching is skipped: every label lands on
    'pending' so the DM can name the speakers manually.
    """
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    audio_uri = payload.get("audio_uri")
    event_segments = payload.get("segments") or []

    if event.type == "transcription.completed" and settings.refiner_enabled:
        # The refiner-service consumes transcription.completed, runs the LLM
        # contextual pass and emits transcription.refined; identifying from the
        # raw labels here would race it. Ack and wait for the refined event.
        logger.info(
            "refiner enabled; session %s will be identified after transcription.refined",
            session_id,
        )
        return

    try:
        await client.update_status(session_id, STATUS_IDENTIFYING_SPEAKERS)
    except ConflictTransition:
        logger.info("session %s already past %s; skipping", session_id, STATUS_IDENTIFYING_SPEAKERS)
        return

    workdir = Path(settings.work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    staged: str | None = None
    try:
        diarization, transcript = await _read_artifacts(
            storage, settings, session_id, event_segments
        )
        segments = diarization.get("segments") or []
        if not segments:
            logger.info("session %s has no diarized segments; nothing to identify", session_id)
            await _finish(session_id, campaign_id, [], client, publisher)
            return

        if not audio_uri:
            # No recording to embed from (anomalous, but the transcript is
            # fine): do not fail the session. Surface every label as pending so
            # the DM can still name the speakers manually from the UI.
            labels = sorted({seg.get("speaker") for seg in segments if seg.get("speaker")})
            logger.warning(
                "session %s has no audio_uri (%s); marking %d label(s) pending "
                "without voice matching",
                session_id,
                event.type,
                len(labels),
            )
            assignments = [
                match_label(
                    label, settings=settings, embedding=None, duration_sec=0.0, hits=[]
                )
                for label in labels
            ]
            await client.upsert_speakers(session_id, assignments)
            await _finish(session_id, campaign_id, assignments, client, publisher)
            return

        suffix = Path(audio_uri).suffix or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=workdir, delete=False) as fh:
            staged = fh.name

        await storage.download_file(settings.minio_recordings_bucket, audio_uri, staged)

        if event.type == "transcription.refined":
            # Labels/text were already fixed by the LLM contextual pass
            # (refiner-service): match the refined labels directly against the
            # enrolled voiceprints, without re-clustering them.
            assignments = await _match_raw(
                segments, staged, settings, voiceprints, embedder, campaign_id
            )
        elif settings.relabel_enabled:
            result = await _relabel(
                staged, segments, settings, voiceprints, embedder, campaign_id
            )
            await _rewrite_artifacts(
                storage, settings, session_id, diarization, transcript, result
            )
            assignments = await _match_relabeled(result, settings, voiceprints, campaign_id)
        else:
            assignments = await _match_raw(
                segments, staged, settings, voiceprints, embedder, campaign_id
            )

        await client.upsert_speakers(session_id, assignments)
        await _finish(session_id, campaign_id, assignments, client, publisher)
        logger.info(
            "session %s speakers identified (%d labels)",
            session_id,
            len(assignments),
        )
    except Exception as exc:
        logger.exception("speaker identification failed for session %s", session_id)
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


async def _read_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    session_id: str,
    event_segments: list[dict],
) -> tuple[dict, dict | None]:
    """Read the transcription artifacts; fall back to the event's segments."""
    diarization: dict | None = None
    transcript: dict | None = None
    dkey = f"transcripts/{session_id}/diarization.json"
    tkey = f"transcripts/{session_id}/transcript.json"
    try:
        diarization = await storage.read_json(settings.minio_transcripts_bucket, dkey)
    except Exception:  # noqa: BLE001 - best-effort read with a documented fallback
        logger.warning("could not read %s; using event segments", dkey)
    try:
        transcript = await storage.read_json(settings.minio_transcripts_bucket, tkey)
    except Exception:  # noqa: BLE001 - best-effort read with a documented fallback
        logger.warning("could not read %s; transcript will not be relabeled", tkey)
    if diarization is None:
        diarization = {"segments": event_segments}
    return diarization, transcript


def _cluster_params(
    settings: ServiceSettings, segments: list[dict]
) -> tuple[int | None, float | None]:
    """(k, cosine-distance threshold) for the re-clustering step."""
    if settings.relabel_speaker_count is not None and settings.relabel_speaker_count > 0:
        return settings.relabel_speaker_count, None
    if settings.relabel_mode == "count":
        raw = {seg.get("speaker") for seg in segments if seg.get("speaker")}
        if raw:
            return len(raw), None
    return None, 1.0 - settings.relabel_merge_threshold


async def _load_anchors(
    voiceprints: VoiceprintStore, campaign_id: str
) -> list[dict]:
    """Per-user enrollment centroids for a campaign (the anchor seeds)."""
    if not campaign_id:
        return []
    try:
        points = await voiceprints.list_campaign(campaign_id)
    except Exception:
        logger.exception("could not list campaign voiceprints; clustering unanchored")
        return []
    by_user: dict[str, list[list[float]]] = {}
    for point in points:
        payload = getattr(point, "payload", None) or {}
        vector = getattr(point, "vector", None)
        user_id = payload.get("user_id")
        if not user_id or not vector:
            continue
        by_user.setdefault(str(user_id), []).append(vector)
    return [
        {"user_id": uid, "embedding": centroid(vectors)}
        for uid, vectors in by_user.items()
    ]


async def _relabel(
    audio_path: str,
    segments: list[dict],
    settings: ServiceSettings,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    campaign_id: str,
) -> RelabelResult:
    """Embed each turn, cluster, and override raw labels (see app.relabel)."""
    turns = speaker_turns(segments)
    if not turns:
        return RelabelResult(
            segments=[dict(seg) for seg in segments],
            labels_by_index=[None] * len(segments),
            labels=[],
            label_embeddings={},
            label_durations={},
            anchors={},
            stats={
                "n_segments": len(segments),
                "n_turns": 0,
                "n_embedded": 0,
                "n_clusters": 0,
                "n_anchored": 0,
                "mode": settings.relabel_mode,
            },
        )

    embed_indices: list[int] = []
    windows: list[tuple[float, float]] = []
    for index, turn in enumerate(turns):
        if turn.end - turn.start >= settings.relabel_min_turn_sec:
            embed_indices.append(index)
            windows.append((turn.start, turn.end))

    turn_embeddings: list[list[float] | None] = [None] * len(turns)
    if windows:
        vectors = await asyncio.to_thread(embedder.embed_windows_sync, audio_path, windows)
        for index, vector in zip(embed_indices, vectors):
            turn_embeddings[index] = vector

    anchors = await _load_anchors(voiceprints, campaign_id)
    k, threshold = _cluster_params(settings, segments)
    return relabel_segments(
        segments,
        turns,
        turn_embeddings,
        threshold=threshold,
        k=k,
        anchors=anchors,
        anchor_threshold=settings.relabel_anchor_threshold,
    )


async def _rewrite_artifacts(
    storage: ObjectStorage,
    settings: ServiceSettings,
    session_id: str,
    diarization: dict,
    transcript: dict | None,
    result: RelabelResult,
) -> None:
    """Write the relabeled diarization + transcript back to MinIO."""
    dkey = f"transcripts/{session_id}/diarization.json"
    new_diarization = dict(diarization)
    new_diarization["segments"] = result.segments
    await storage.put_json(settings.minio_transcripts_bucket, dkey, new_diarization)

    if transcript is None:
        return
    span_label: dict[tuple[float, float], str] = {}
    for seg, label in zip(diarization.get("segments") or [], result.labels_by_index):
        if label is not None:
            span_label[(seg.get("start"), seg.get("end"))] = label
    for seg in transcript.get("segments") or []:
        label = span_label.get((seg.get("start"), seg.get("end")))
        if label is not None:
            seg["speaker"] = label
    tkey = f"transcripts/{session_id}/transcript.json"
    await storage.put_json(settings.minio_transcripts_bucket, tkey, transcript)


async def _match_relabeled(
    result: RelabelResult,
    settings: ServiceSettings,
    voiceprints: VoiceprintStore,
    campaign_id: str,
) -> list[dict]:
    """Match each new label to an enrolled voiceprint (anchors first)."""
    assignments: list[dict] = []
    for label in result.labels:
        if label in result.anchors:
            user_id, score = result.anchors[label]
            assignments.append(
                {
                    "speaker_label": label,
                    "user_id": user_id,
                    "confidence": round(score, 4),
                    "status": "auto",
                }
            )
            continue
        embedding = result.label_embeddings.get(label)
        duration = result.label_durations.get(label, 0.0)
        hits: list = []
        if embedding is not None:
            hits = await voiceprints.search(embedding, campaign_id, limit=1)
        assignments.append(
            match_label(
                label,
                settings=settings,
                embedding=embedding,
                duration_sec=duration,
                hits=hits,
            )
        )
    return assignments


async def _match_raw(
    segments: list[dict],
    audio_path: str,
    settings: ServiceSettings,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    campaign_id: str,
) -> list[dict]:
    """Legacy path (relabel_enabled=False): embed one window per raw label."""
    windows = speaker_windows(segments, settings.speaker_pool_sec)
    assignments: list[dict] = []
    for label in sorted(windows):
        start, end = windows[label]
        embedding: list[float] | None = None
        duration = 0.0
        try:
            embedding, duration = await asyncio.to_thread(
                _embed_sync, audio_path, start, end, embedder
            )
        except Exception:
            logger.exception("could not embed speaker %s (window %.2f-%.2f)", label, start, end)
        hits: list = []
        if embedding is not None:
            hits = await voiceprints.search(embedding, campaign_id, limit=1)
        assignments.append(
            match_label(
                label,
                settings=settings,
                embedding=embedding,
                duration_sec=duration,
                hits=hits,
            )
        )
    return assignments


async def process_assigned(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
) -> None:
    """DM named a speaker: enroll a session-derived voiceprint for that user.

    The enrolled point becomes one of the "old, named" embeddings that later
    sessions are compared against, so the name is reused automatically.
    """
    payload = event.payload
    session_id = str(payload.get("session_id", ""))
    campaign_id = str(payload.get("campaign_id", ""))
    label = str(payload.get("label", ""))
    user_id = payload.get("user_id")
    audio_uri = payload.get("audio_uri")

    if not (session_id and campaign_id and label and audio_uri):
        logger.warning("speakers.assigned missing fields; skipping enrollment: %s", payload)
        return
    if not user_id:
        # A userless member (no linked account) was named: there is no
        # user-keyed voiceprint to enroll, so later sessions cannot
        # auto-match this voice by name yet. The assignment itself is
        # already stored; only the enrollment step is skipped.
        logger.info(
            "speaker %s named without a linked user; skipping voiceprint enrollment",
            label,
        )
        return

    workdir = Path(settings.work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio_uri).suffix or ".audio"
    staged: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=workdir, delete=False) as fh:
            staged = fh.name
        await storage.download_file(settings.minio_recordings_bucket, audio_uri, staged)

        diarization = await storage.read_json(
            settings.minio_transcripts_bucket, f"transcripts/{session_id}/diarization.json"
        )
        windows = speaker_windows(diarization.get("segments", []), settings.speaker_pool_sec)
        if label not in windows:
            logger.warning(
                "no diarized window for label %s in session %s; skipping enrollment", label, session_id
            )
            return
        start, end = windows[label]

        embedding, duration = await asyncio.to_thread(_embed_sync, staged, start, end, embedder)
        if duration < settings.voice_sample_min_sec:
            logger.warning(
                "clip for label %s too short (%.1fs); skipping enrollment", label, duration
            )
            return

        await voiceprints.upsert(
            str(uuid.uuid4()),
            embedding,
            {
                "user_id": str(user_id),
                "campaign_id": str(campaign_id),
                # pseudo-uri: the session recording + label the print was derived from
                "sample_uri": f"recordings/{session_id}#{label}",
                "version": settings.embedding_version,
                "source": "session",
                "session_id": session_id,
                "speaker_label": label,
            },
        )
        logger.info(
            "enrolled session-derived voiceprint for user %s (label %s, session %s)",
            user_id,
            label,
            session_id,
        )
    finally:
        if staged is not None:
            try:
                os.unlink(staged)
            except OSError:  # already gone / permission
                pass


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: dispatch on event type with process-level singletons."""
    settings = _get_settings()
    storage = get_storage(settings)
    voiceprints = get_voiceprints(settings)
    embedder = get_embedder(settings)

    if event.type == "speakers.assigned":
        await process_assigned(event, settings, storage, voiceprints, embedder)
        return

    client = get_client(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_identified(event, settings, storage, client, voiceprints, embedder, publisher)


async def main() -> None:
    settings = _get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(
        connection,
        "speakers.identify",
        lambda event: handle(event, connection),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
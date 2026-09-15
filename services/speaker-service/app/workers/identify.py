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
from dnd_common.purity import assess_purity

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
from app.embedding import VoiceEmbedder, WindowAnalysis
from app.evidence import (
    CentroidHit,
    aggregate_candidate_scores,
    evidence_payload,
    hits_from_points,
    observation_evidence,
)
from app.history import HistoryEntry, HistorySample, manual_samples
from app.identify import anchor_verdict, match_label
from app.observations import Observation, observation_point_id, plan_observations
from app.qdrant import MemberVoiceModelStore, VoiceObservationStore, VoiceprintStore
from app.quality import assess
from app.relabel import RelabelResult, centroid, relabel_segments, speaker_turns
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_client: SessionServiceClient | None = None
_voiceprints: VoiceprintStore | None = None
_embedder: VoiceEmbedder | None = None
_member_models: MemberVoiceModelStore | None = None
_observations: VoiceObservationStore | None = None


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


def get_member_models(settings: ServiceSettings) -> MemberVoiceModelStore:
    global _member_models
    if _member_models is None:
        _member_models = MemberVoiceModelStore(settings)
    return _member_models


def get_observations(settings: ServiceSettings) -> VoiceObservationStore:
    global _observations
    if _observations is None:
        _observations = VoiceObservationStore(settings)
    return _observations


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
    evidence: dict | None = None,
) -> None:
    """Park the session on the DM's speaker decisions, then publish the result.

    An 'auto' match is a *proposal*: the DM accepts it (or names someone else)
    in the speaker panel. The session therefore stays on 'speaker_pending' until
    every label is confirmed - that is what makes the names, and the voice
    samples learned from them, trustworthy before the next pipeline step runs.
    A session with nothing left to decide (no diarized labels at all) closes the
    stage on its own.
    """
    unconfirmed = [a["speaker_label"] for a in assignments if a["status"] != "confirmed"]
    # Only these need a NAME; the rest just need the DM to accept the match.
    unnamed = [a["speaker_label"] for a in assignments if a["status"] == "pending"]
    if unconfirmed:
        await client.update_status(session_id, STATUS_SPEAKER_PENDING)
        await publisher(
            Event(
                type="speaker.pending",
                payload={
                    "campaign_id": campaign_id,
                    "session_id": session_id,
                    "pending_labels": unnamed,
                    "unconfirmed_labels": unconfirmed,
                },
            )
        )
    else:
        await client.update_status(session_id, STATUS_SPEAKERS_IDENTIFIED)
    payload: dict = {
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
        "pending_assignment": bool(unconfirmed),
    }
    if evidence is not None:
        # The redesign's consumers read 'evidence' (per-observation nearest
        # centroids); the legacy 'speakers' verdicts stay for one release so the
        # old path keeps working unchanged (docs/attribution-plan.md S8).
        payload["evidence"] = evidence
    await publisher(Event(type="speakers.identified", payload=payload))


async def process_identified(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    publisher: Callable[[Event], Awaitable[None]],
    *,
    member_models: MemberVoiceModelStore | None = None,
    observations_store: VoiceObservationStore | None = None,
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

        # Before matching: turn the DM's manual namings in earlier sessions of
        # this campaign into voice samples, so this session is identified
        # against them too (best effort - never fails the run).
        await _enroll_history_samples(
            settings,
            storage,
            client,
            voiceprints,
            embedder,
            campaign_id,
            session_id,
        )

        # The observations must carry the SAME labels the verdicts and the
        # rewritten artifacts use. Re-clustering overrides the raw diarizer
        # labels, so observing the original segments would file the engine's
        # voice evidence under labels that no longer exist anywhere else.
        observed_segments = segments
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
            observed_segments = result.segments
        else:
            assignments = await _match_raw(
                segments, staged, settings, voiceprints, embedder, campaign_id
            )

        evidence = None
        if settings.observations_enabled:
            evidence = await _observe(
                staged,
                observed_segments,
                settings=settings,
                session_id=session_id,
                campaign_id=campaign_id,
                voiceprints=voiceprints,
                embedder=embedder,
                member_models=member_models,
                observations_store=observations_store,
            )
            assignments = _refine_assignments(assignments, evidence, settings=settings)

        await client.upsert_speakers(session_id, assignments)
        await _finish(session_id, campaign_id, assignments, client, publisher, evidence)
        logger.info(
            "session %s speakers identified (%d labels, %d observations)",
            session_id,
            len(assignments),
            len((evidence or {}).get("observations", [])),
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


async def _enrolled_history_keys(voiceprints: VoiceprintStore, campaign_id: str) -> set[str]:
    """Sample keys already in the campaign store (idempotency guard)."""
    points = await voiceprints.list_campaign(campaign_id)
    keys: set[str] = set()
    for point in points:
        payload = getattr(point, "payload", None) or {}
        key = payload.get("history_key")
        if key:
            keys.add(str(key))
    return keys


def _history_point_id(key: str) -> str:
    """Deterministic Qdrant id of one manual sample (re-runs overwrite it)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dnd:history-sample:{key}"))


async def _enroll_history_samples(
    settings: ServiceSettings,
    storage: ObjectStorage,
    client: SessionServiceClient,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    campaign_id: str,
    session_id: str,
) -> int:
    """Enroll voice samples from the DM's manual namings in earlier sessions.

    The DM naming a speaker is a labelled sample: the labelled, long-enough and
    confident turns of earlier sessions of the same campaign (see app.history)
    are embedded and stored as campaign voiceprints, so this session is matched
    against every naming the campaign already has - not only against profile
    enrollment and the print the naming event happened to write. Each
    (session, label, window) is stored at most once (deterministic point id +
    history_key payload marker), so re-identifying a session is cheap.

    Best effort by design: a missing artifact, an unreachable session-service
    or a failed embedding leaves the campaign voiceprints as they were and
    never fails the identification run. Returns the number of prints written.
    """
    if not (settings.history_samples_enabled and campaign_id):
        return 0
    try:
        already = await _enrolled_history_keys(voiceprints, campaign_id)
        entries = await client.speaker_history(
            campaign_id,
            exclude_session_id=session_id,
            limit_sessions=settings.history_max_sessions,
        )
    except Exception:
        # History is an enhancement, not a requirement: identify as before.
        logger.exception("could not read the campaign speaker history; continuing without it")
        return 0

    history: list[HistoryEntry] = []
    audio_uris: dict[str, str] = {}
    for entry in entries:
        entry_session = str(entry.get("session_id") or "")
        label = str(entry.get("speaker_label") or "")
        user_id = entry.get("user_id")
        audio_uri = entry.get("audio_uri")
        if not (entry_session and label and user_id and audio_uri):
            continue
        history.append(
            HistoryEntry(session_id=entry_session, label=label, user_id=str(user_id))
        )
        audio_uris[entry_session] = str(audio_uri)
    if not history:
        return 0

    # The diarization artifacts tell which parts of those recordings the DM's
    # labels actually cover (and how confident the diarizer was about them).
    diarizations: dict[str, list[dict]] = {}
    for history_session in {entry.session_id for entry in history}:
        key = f"transcripts/{history_session}/diarization.json"
        try:
            document = await storage.read_json(settings.minio_transcripts_bucket, key)
        except Exception:  # noqa: BLE001 - skip this session, keep the others
            logger.warning("no diarization for session %s; its samples are skipped", history_session)
            continue
        diarizations[history_session] = document.get("segments") or []

    samples = manual_samples(
        history,
        diarizations,
        already_enrolled=already,
        min_sec=settings.history_sample_min_sec,
        max_sec=settings.history_sample_max_sec,
        min_confidence=settings.history_sample_min_confidence,
        max_windows_per_label=settings.history_max_windows_per_label,
        max_windows_per_run=settings.history_max_windows_per_run,
    )
    if not samples:
        return 0

    workdir = Path(settings.work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    by_session: dict[str, list[HistorySample]] = {}
    for sample in samples:
        by_session.setdefault(sample.session_id, []).append(sample)

    written = 0
    for history_session, session_samples in by_session.items():
        staged: str | None = None
        vectors: list[list[float] | None]
        try:
            suffix = Path(audio_uris[history_session]).suffix or ".audio"
            with tempfile.NamedTemporaryFile(suffix=suffix, dir=workdir, delete=False) as fh:
                staged = fh.name
            await storage.download_file(
                settings.minio_recordings_bucket, audio_uris[history_session], staged
            )
            windows = [(s.window.start, s.window.end) for s in session_samples]
            # One decode, many windows: the recording is loaded once per session.
            vectors = await asyncio.to_thread(embedder.embed_windows_sync, staged, windows)
        except Exception:
            # One unreadable session must not stop the others (or the run).
            logger.exception("could not embed the labelled turns of session %s", history_session)
            continue
        finally:
            if staged is not None:
                try:
                    os.unlink(staged)
                except OSError:  # already gone / permission
                    pass

        for sample, vector in zip(session_samples, vectors):
            if vector is None or sample.window.duration < settings.voice_sample_min_sec:
                continue
            await voiceprints.upsert(
                _history_point_id(sample.key),
                vector,
                {
                    "user_id": sample.user_id,
                    "campaign_id": str(campaign_id),
                    # pseudo-uri: recording + label + window the print came from
                    "sample_uri": f"recordings/{sample.session_id}#{sample.label}#{sample.window.index}",
                    "version": settings.embedding_version,
                    "source": "history",
                    "session_id": sample.session_id,
                    "speaker_label": sample.label,
                    "history_key": sample.key,
                    "turn_confidence": sample.window.confidence,
                },
            )
            written += 1

    if written:
        logger.info(
            "enrolled %d voice sample(s) from %d named session(s) of campaign %s",
            written,
            len(by_session),
            campaign_id,
        )
    return written


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
                anchor_verdict(label, user_id=user_id, score=score, settings=settings)
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


def _refine_assignments(
    assignments: list[dict], evidence: dict, *, settings: ServiceSettings
) -> list[dict]:
    """Re-derive each legacy verdict from the per-observation evidence.

    A pooled label average and the label's best observation can disagree: a
    label holding one clear speaker and one badly captured guest averages to a
    mediocre cosine, while its best observation is a confident match. Since the
    engine sees observations, the compatibility verdicts are computed the same
    way, so the panel the DM still uses shows the number the engine actually
    acted on and the two cannot drift apart.
    """
    records = evidence.get("observations") or []
    if not records:
        return assignments
    by_label = _observation_label_scores(
        records,
        top_k=settings.member_score_top_k,
        mode=settings.member_score_mode,
    )
    if not by_label:
        return assignments

    refined: list[dict] = []
    for assignment in assignments:
        bucket = by_label.get(str(assignment.get("speaker_label")))
        if not bucket:
            refined.append(assignment)
            continue
        candidate, score = max(bucket.items(), key=lambda item: item[1])
        # Only a user-keyed candidate can be expressed in the legacy
        # speaker_assignments shape (it stores user_id, not member_id).
        user_id = candidate.split(":", 1)[1] if candidate.startswith("user:") else None
        matched = score >= settings.speaker_match_threshold
        refined.append(
            {
                **assignment,
                "user_id": user_id if matched else assignment.get("user_id"),
                "confidence": round(score, 4),
                "status": "auto" if matched and user_id else assignment.get("status", "pending"),
            }
        )
    return refined


async def _centroid_hits(
    embedding: list[float],
    *,
    settings: ServiceSettings,
    voiceprints: VoiceprintStore,
    member_models: MemberVoiceModelStore | None,
    campaign_id: str,
) -> list[CentroidHit]:
    """Every campaign centroid that could own this observation, best first.

    Both stores are searched on purpose. member_voice_models holds the
    per-centroid models written from now on (member-keyed, multi-centroid);
    voiceprints holds the legacy account-keyed prints plus the history samples.
    Merging them means the transition needs no re-enrollment, and an old print
    keeps contributing until a better one supersedes it.
    """
    hits: list[CentroidHit] = []
    if member_models is not None and campaign_id:
        try:
            points = await member_models.search(
                embedding, campaign_id, limit=settings.evidence_max_centroids
            )
            hits.extend(hits_from_points(points, centroid_prefix="model"))
        except Exception:
            logger.exception("member voice model search failed; using voiceprints only")
    if campaign_id:
        try:
            points = await voiceprints.search(
                embedding, campaign_id, limit=settings.evidence_max_centroids
            )
            hits.extend(hits_from_points(points, centroid_prefix="voiceprint"))
        except Exception:
            logger.exception("voiceprint search failed for campaign %s", campaign_id)
    return hits


async def _observe(
    audio_path: str,
    segments: list[dict],
    *,
    settings: ServiceSettings,
    session_id: str,
    campaign_id: str,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    member_models: MemberVoiceModelStore | None = None,
    observations_store: VoiceObservationStore | None = None,
) -> dict:
    """Embed every speaker turn, store it, and report what each one matches.

    This is the phase-0 replacement for 'one pooled window per label, one
    cosine, one verdict' (docs/attribution-plan.md S2). Each turn becomes an
    observation with its own vector, its own quality score and its own list of
    nearest centroids; the noise a label-level average used to hide (one label
    holding several people, one person spread over several labels) is exactly
    what the engine now has the data to detect.

    Returns the 'evidence' block of the speakers.identified payload. Never
    raises: a missing store or an unreadable recording downgrades the evidence
    to 'no voice data' rather than failing an otherwise good identification.
    """
    empty = evidence_payload(
        [],
        model_version=settings.voice_embedding_model,
        embedding_version=settings.embedding_version,
    )
    batch = plan_observations(segments, min_sec=settings.observation_min_sec)
    if not batch.observations:
        return empty

    windows = [
        (batch.observations[index].start, batch.observations[index].end)
        for index in batch.embeddable
    ]
    analyses: list[WindowAnalysis] = []
    if windows:
        try:
            analyses = await asyncio.to_thread(
                embedder.embed_windows_full_sync, audio_path, windows
            )
        except Exception:
            logger.exception(
                "could not embed the observations of session %s; text evidence only",
                session_id,
            )
            analyses = []
    analysis_by_index = {index: a for index, a in zip(batch.embeddable, analyses)}

    points: list[tuple[str, list[float], dict]] = []
    records: list[dict] = []
    for observation in batch.observations:
        analysis = analysis_by_index.get(observation.index)
        embedding = analysis.embedding if analysis is not None else None
        quality: float | None = None
        hits: list[CentroidHit] = []
        if embedding is not None and analysis is not None:
            verdict = assess(
                observation.duration,
                snr_db=analysis.snr_db,
                overlap_ratio=analysis.overlap_ratio,
                min_sec=settings.observation_min_sec,
                full_sec=settings.observation_full_sec,
                floor=settings.observation_quality_floor,
            )
            quality = verdict.score
            hits = await _centroid_hits(
                embedding,
                settings=settings,
                voiceprints=voiceprints,
                member_models=member_models,
                campaign_id=campaign_id,
            )
            points.append(
                (
                    observation_point_id(session_id, observation.label, observation.index),
                    embedding,
                    {
                        "campaign_id": str(campaign_id),
                        "session_id": str(session_id),
                        "observation_id": observation.key,
                        "label": observation.label,
                        "index": observation.index,
                        "start_sec": round(observation.start, 3),
                        "end_sec": round(observation.end, 3),
                        "diar_confidence": observation.diar_confidence,
                        "model_version": settings.voice_embedding_model,
                        "embedding_version": settings.embedding_version,
                        **verdict.as_payload(),
                    },
                )
            )
        records.append(
            observation_evidence(
                observation_id=observation.key,
                label=observation.label,
                index=observation.index,
                start=observation.start,
                end=observation.end,
                quality=quality,
                hits=hits,
                limit=settings.evidence_max_centroids,
                floor=settings.evidence_cosine_floor,
            )
        )

    if observations_store is not None and points:
        try:
            await observations_store.upsert_many(points)
        except Exception:
            logger.exception(
                "could not store the voice observations of session %s", session_id
            )
    return evidence_payload(
        records,
        model_version=settings.voice_embedding_model,
        embedding_version=settings.embedding_version,
    )


def _observation_label_scores(
    records: list[dict], *, top_k: int, mode: str
) -> dict[str, dict[str, float]]:
    """Per-input-label aggregate of a member's evidence, for the legacy verdicts.

    The legacy panel still needs one number per diarized label. Instead of the
    pooled cosine it used to compute, the number is now the top-k aggregate of
    every observation inside that label, which is the same evidence the engine
    consumes - so the panel and the engine can no longer disagree about how good
    a match is.
    """
    scores: dict[str, dict[str, float]] = {}
    for record in records:
        hits = [
            CentroidHit(
                centroid_id=str(hit.get("centroid_id", "?")),
                cosine=float(hit.get("cosine", 0.0)),
                member_id=hit.get("member_id"),
                user_id=hit.get("user_id"),
                quality=hit.get("quality"),
                source=hit.get("source"),
            )
            for hit in record.get("nearest_centroids", [])
        ]
        if not hits:
            continue
        per_observation = aggregate_candidate_scores(hits, top_k=top_k, mode=mode)
        bucket = scores.setdefault(str(record.get("label", "")), {})
        for candidate, score in per_observation.items():
            bucket[candidate] = max(bucket.get(candidate, 0.0), score)
    return scores


async def process_assigned(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    *,
    member_models: MemberVoiceModelStore | None = None,
) -> None:
    """DM named a speaker: enroll what that name is worth as voice evidence.

    The old contract was 'one user-keyed voiceprint per named label, embedded
    from the label's pooled window'. Two defects made that poison later sessions
    (attribution-model S12.3):

    1. a pooled window of a label that held two people enrolls a *mixture* that
       matches neither of them, and every later session inherits the error;
    2. the print was keyed on user_id, so a campaign member without a linked
       account was logged as 'skipping voiceprint enrollment' and could never
       auto-match in any later session, however often the DM named them.

    Now every observation of the named label is embedded, gated on its own
    quality and on the identity's purity, and stored as its own centroid keyed
    on member_id. A member with several voice modes accumulates several
    centroids and is scored against the best of them (S12.2).
    """
    payload = event.payload
    session_id = str(payload.get("session_id", ""))
    campaign_id = str(payload.get("campaign_id", ""))
    label = str(payload.get("label", ""))
    user_id = payload.get("user_id")
    member_id = payload.get("member_id")
    audio_uri = payload.get("audio_uri")
    purity_hint = payload.get("identity_purity")

    if not (session_id and campaign_id and label and audio_uri):
        logger.warning("speakers.assigned missing fields; skipping enrollment: %s", payload)
        return
    if not (user_id or member_id):
        logger.warning(
            "speaker %s named without a member or user link; nothing to enroll", label
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
        written = await _enroll_observations(
            staged,
            diarization.get("segments", []),
            label=label,
            session_id=session_id,
            campaign_id=campaign_id,
            user_id=str(user_id) if user_id else None,
            member_id=str(member_id) if member_id else None,
            purity_hint=(
                float(purity_hint)
                if isinstance(purity_hint, (int, float)) and not isinstance(purity_hint, bool)
                else None
            ),
            settings=settings,
            voiceprints=voiceprints,
            embedder=embedder,
            member_models=member_models,
        )
        if written:
            logger.info(
                "enrolled %d observation centroid(s) for member %s (label %s, session %s)",
                written,
                member_id or user_id,
                label,
                session_id,
            )
    finally:
        if staged is not None:
            try:
                os.unlink(staged)
            except OSError:  # already gone / permission
                pass


async def _enroll_observations(
    audio_path: str,
    segments: list[dict],
    *,
    label: str,
    session_id: str,
    campaign_id: str,
    user_id: str | None,
    member_id: str | None,
    purity_hint: float | None,
    settings: ServiceSettings,
    voiceprints: VoiceprintStore,
    embedder: VoiceEmbedder,
    member_models: MemberVoiceModelStore | None,
) -> int:
    """Embed and store the gated observations of one DM-named label.

    Gate order (cheapest first): the label must exist, its observations must be
    long enough to embed, the identity must look pure, and each individual
    observation must clear the quality floor. An impure identity enrolls
    NOTHING: a mixture print is worse than no print, because it silently
    poisons every later session of the campaign.
    """
    batch = plan_observations(segments, min_sec=settings.voice_sample_min_sec)
    mine = [o for o in batch.observations if o.label == label]
    embeddable = [o for o in mine if o.index in set(batch.embeddable)]
    if not embeddable:
        logger.warning(
            "no diarized turn long enough for label %s in session %s; skipping enrollment",
            label,
            session_id,
        )
        return 0

    windows = [(o.start, o.end) for o in embeddable]
    analyses = await asyncio.to_thread(embedder.embed_windows_full_sync, audio_path, windows)

    vectors: list[list[float]] = []
    durations: list[float] = []
    graded: list[tuple[Observation, WindowAnalysis, float]] = []
    for observation, analysis in zip(embeddable, analyses):
        if analysis.embedding is None:
            continue
        verdict = assess(
            observation.duration,
            snr_db=analysis.snr_db,
            overlap_ratio=analysis.overlap_ratio,
            min_sec=settings.voice_sample_min_sec,
            full_sec=settings.observation_full_sec,
            floor=settings.enroll_min_quality,
        )
        vectors.append(analysis.embedding)
        durations.append(observation.duration)
        graded.append((observation, analysis, verdict.score))

    if not vectors:
        logger.warning("nothing embeddable for label %s in session %s", label, session_id)
        return 0

    purity = purity_hint
    if purity is None:
        purity = assess_purity(vectors, durations=durations).purity
    if purity < settings.enroll_min_purity:
        logger.warning(
            "label %s in session %s looks impure (purity %.2f); enrolling nothing "
            "rather than poisoning the campaign with a mixture print",
            label,
            session_id,
            purity,
        )
        return 0

    written = 0
    for observation, analysis, quality in graded:
        if quality < settings.enroll_min_quality:
            logger.info(
                "skipping observation %s (quality %.2f below the enrollment floor)",
                observation.key,
                quality,
            )
            continue
        assert analysis.embedding is not None
        payload = {
            "campaign_id": campaign_id,
            "source": "session",
            "session_id": session_id,
            "speaker_label": label,
            "observation_id": observation.key,
            "sample_uri": f"recordings/{session_id}#{observation.key}",
            "version": settings.embedding_version,
            "quality": round(quality, 4),
            "weight": round(quality, 4),
            "identity_purity": round(purity, 4),
            "start_sec": round(observation.start, 3),
            "end_sec": round(observation.end, 3),
        }
        if member_id:
            payload["member_id"] = member_id
        if user_id:
            payload["user_id"] = user_id

        if member_models is not None and member_id:
            point_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"dnd:member-voice-model:{campaign_id}:{member_id}:{session_id}:{observation.key}",
                )
            )
            await member_models.upsert(point_id, analysis.embedding, payload)
        if user_id:
            # Legacy collection: still written so the transition needs no
            # big-bang re-enrollment, and so anything still reading voiceprints
            # (the history backfill, the old panel) sees the new samples.
            await voiceprints.upsert(str(uuid.uuid4()), analysis.embedding, payload)
        written += 1
    return written


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: dispatch on event type with process-level singletons."""
    settings = _get_settings()
    storage = get_storage(settings)
    voiceprints = get_voiceprints(settings)
    embedder = get_embedder(settings)
    member_models = get_member_models(settings)
    observations_store = get_observations(settings)

    if event.type == "speakers.assigned":
        await process_assigned(
            event, settings, storage, voiceprints, embedder, member_models=member_models
        )
        return

    client = get_client(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    await process_identified(
        event,
        settings,
        storage,
        client,
        voiceprints,
        embedder,
        publisher,
        member_models=member_models,
        observations_store=observations_store,
    )


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
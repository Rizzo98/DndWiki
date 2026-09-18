"""Content pipeline worker: two review layers, then the wiki write.

Consumes the 'content.generate' queue. Five event types are routed by
dnd_common.events.TOPOLOGY:

- speakers.identified / speakers.assigned -> PHASE 1 (process_job): download
  the named transcript, extract structure per chunk, merge, and persist a
  DRAFT session summary. NO page, NO event and NO timeline entry is created
  here: the summary is the first review layer.
- summary.regenerate -> PHASE 1b (process_summary_regeneration): apply the
  DM's review feedback (highlighted passages of the narrative + what must
  change) to the persisted extraction. The model answers with a PATCH - the
  complete narrative
  plus the items the correction touches, addressed by the id each item was
  given - and the worker merges it in (app/revision.py), so a correction is
  never half-applied and never silently dropped: an answer that does not fit
  the extraction fails the job instead.
- summary.confirmed -> PHASE 2 (process_plan_generation): the DM accepted the
  summary, so it is turned into a PROPOSED change set (pages to create, pages
  to update, timeline entries, cross-references) and stored for review.
  STILL nothing is written to the wiki.
- plan.confirmed -> PHASE 3 (process_plan_application): the DM reviewed,
  edited and confirmed the change set, so it is written into the wiki through
  wiki-service: the pages land PUBLISHED and the timeline entries APPROVED.

Pipeline (per session):

1. Skip sessions that still have pending speaker assignments (a later
   speakers.assigned event re-triggers) and ignore late speaker events for a
   session that is already past the summary phase (the DM's reviewed summary
   is never silently overwritten).
2. Move the session to 'summarizing' via the session-service internal API; a
   409 (already summarizing / already past it) means an earlier delivery
   handled it -> ack and skip (idempotency guard).
3. Download the named transcript from MinIO
   (transcripts/<session_id>/transcript.json), resolve speaker labels to
   player display names (user-service), and build a compact '[HH:MM:SS]
   NAME: text' view.
4. Split into overlapping token-bounded chunks; extract structured JSON from
   each chunk via LiteLLM (bounded concurrency).
5. Merge across chunks (dedupe by name, longest description, summed mentions,
   beats concatenated in chunk order), write the session's STORY from them
   (SUMMARY_COMPOSE_PROMPT -> the narrative in scene blocks) and persist the
   result in 'session_summaries' as a draft (revision 1). The DM reads it and
   highlights the passages to correct; each rewrite bumps the revision.
6. PHASE 2 expands the confirmed summary into the change set (cross-session
   dedupe: an entity the campaign already documents is skipped, not
   re-proposed) and records the generation_jobs row.
7. PHASE 3 applies the confirmed change set, records the run and publishes
   content.generated.

Failure semantics mirror transcription-service: failures mark the session
'failed' (recorded) and re-raise; the redelivered copy re-runs the phase (the
state machine accepts failed -> summarizing/generating_wiki/applying_wiki) or
is acked as a no-op, so a failed job is never re-run blindly or DLQ-spammed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import aio_pika
from dnd_common.db import session_factory
from dnd_common.events import Event, connect_rabbitmq, consume, publish
from sqlalchemy.ext.asyncio import AsyncSession

from app import services as job_services
from app.attribution import apply_gate
from app.chunking import chunk_artifact, chunk_transcript, stretch_note
from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import (
    STATUS_APPLYING_WIKI,
    STATUS_CONTENT_READY,
    STATUS_FAILED,
    STATUS_GENERATING_WIKI,
    STATUS_SUMMARIZING,
    STATUS_SUMMARY_READY,
    STATUS_WIKI_PLAN_READY,
    ConflictTransition,
    SessionServiceClient,
)
from app.clients.user_service import UserServiceClient
from app.clients.wiki_service import WikiServiceClient, WikiServiceError
from app.core.config import ServiceSettings, get_settings
from app.extraction import LLMClient
from app.merger import (
    exclude_character_names,
    is_narrator_name,
    merge_extractions,
)
from app.models import PHASE_APPLY, PHASE_SUMMARY, PHASE_WIKI, PLAN_APPLIED
from app.planner import build_change_set
from app.revision import apply_summary_revision, describe_revision
from app.services.summaries import summary_lines
from app.storage import ObjectStorage
from app.summary import blocks_to_text, describe_blocks

logger = logging.getLogger(__name__)

#: Session statuses a transcript distillation may start from: a freshly
#: identified session, or one whose distillation failed and whose message is
#: being redelivered. Anything else (a session the DM is reviewing, or one
#: that already generated its wiki) ignores late speakers.* events — re-running
#: phase 1 there would throw away the DM's corrections or duplicate pages.
#: Statuses the summary phase may START from, per mode.
#:
#: Without the attribution engine this is the old path: speaker identification
#: finishes and the summary begins.
#:
#: With the engine ON the REVIEW is the gate instead, and 'speakers_identified'
#: must NOT start the summary any more - both services subscribe to
#: 'speakers.identified', so leaving it in lets content-service and the engine
#: race for the same session, and the loser's work is silently dropped.
#:
#: 'attribution_review' is the DM pressing Finish; 'attribution_ready' is the
#: same thing with nothing worth asking (the engine publishes
#: 'attribution.review.completed' for both). 'failed' is kept in both modes so a
#: broken phase can still be summarised from what the transcript does have.
SUMMARY_SOURCE_STATUSES = frozenset({"speakers_identified", "failed"})
ATTRIBUTION_SOURCE_STATUSES = frozenset(
    {"attribution_ready", "attribution_review", "failed"}
)

#: Correlated event type published whenever a (new or rewritten) draft summary
#: is ready for the DM to review.
SUMMARY_DRAFTED = "content.summary.drafted"

#: Correlated event type published when the proposed change set of a session
#: is stored and waiting for the DM's review.
PLAN_READY = "content.plan.ready"

_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None
_session_client: SessionServiceClient | None = None
_user_client: UserServiceClient | None = None
_wiki_client: WikiServiceClient | None = None
_campaign_client: CampaignServiceClient | None = None
_llm: LLMClient | None = None


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


def get_session_client(settings: ServiceSettings) -> SessionServiceClient:
    global _session_client
    if _session_client is None:
        _session_client = SessionServiceClient(settings)
    return _session_client


def get_user_client(settings: ServiceSettings) -> UserServiceClient:
    global _user_client
    if _user_client is None:
        _user_client = UserServiceClient(settings)
    return _user_client


def get_wiki_client(settings: ServiceSettings) -> WikiServiceClient:
    global _wiki_client
    if _wiki_client is None:
        _wiki_client = WikiServiceClient(settings)
    return _wiki_client


def get_campaign_client(settings: ServiceSettings) -> CampaignServiceClient:
    global _campaign_client
    if _campaign_client is None:
        _campaign_client = CampaignServiceClient(settings)
    return _campaign_client


def get_llm(settings: ServiceSettings) -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient(settings)
    return _llm


def build_speaker_map(event: Event) -> dict[str, dict[str, str | None]]:
    """Speaker label -> {"user_id", "display_name", "character_name"}.

    speakers.identified carries the full map (user-keyed, from voiceprint
    matches); speakers.assigned carries only the one label the DM just
    named, with display_name when the member has no linked user account.
    character_name is the member's CHARACTER - generation labels party
    speakers by their character, never by the player name. Unknown labels
    keep the raw diarization label in the transcript view.
    """
    payload = event.payload
    if event.type == "speakers.identified":
        return {
            str(s["label"]): {
                "user_id": str(s["user_id"]) if s.get("user_id") else None,
                # userless members keep the player_name the session-service
                # attached (first run via speakers.assigned, re-runs via the
                # internal speakers listing).
                "display_name": s.get("display_name"),
                "character_name": s.get("character_name"),
            }
            for s in payload.get("speakers", [])
            if s.get("label") and (s.get("user_id") or s.get("display_name"))
        }
    if event.type == "speakers.assigned":
        label, user_id = payload.get("label"), payload.get("user_id")
        if label and (user_id or payload.get("display_name")):
            return {
                str(label): {
                    "user_id": str(user_id) if user_id else None,
                    "display_name": payload.get("display_name"),
                    "character_name": payload.get("character_name"),
                }
            }
    return {}


async def resolve_speaker_names(
    speaker_map: dict[str, dict[str, str | None]], user_client: UserServiceClient
) -> dict[str, str]:
    """label -> display name for the named-transcript view.

    Party members are labeled by their CHARACTER name whenever the campaign
    member data provides one (wiki convention: never the player name);
    otherwise linked users resolve through user-service, userless members
    fall back to the display_name the session-service attached, and anything
    else keeps the raw label.
    """
    if not speaker_map:
        return {}
    user_ids = [info["user_id"] for info in speaker_map.values() if info.get("user_id")]
    resolved = await user_client.display_names(user_ids)
    names: dict[str, str] = {}
    for label, info in speaker_map.items():
        if info.get("character_name"):
            names[label] = str(info["character_name"])
        elif info.get("user_id") and info["user_id"] in resolved:
            names[label] = resolved[info["user_id"]]
        elif info.get("display_name"):
            names[label] = str(info["display_name"])
        else:
            names[label] = label
    return names


def party_character_names(
    speaker_map: dict[str, dict[str, str | None]],
) -> list[str]:
    """CHARACTER names carried by the speaker map (player-character tags)."""
    return [
        str(info["character_name"])
        for info in speaker_map.values()
        if info.get("character_name")
    ]


async def _dm_speaker_names(
    speaker_map: dict[str, dict[str, str | None]],
    speaker_names: dict[str, str],
    campaign_client: CampaignServiceClient | None,
    campaign_id: str,
) -> list[str]:
    """Resolved names of the campaign DM's speaker(s); [] when unknown.

    The DM's speaker carries the DM user id in the speaker map; that user id
    comes from campaign-service (best-effort — a failure only means the
    hardcoded narrator net in the merger has to do the job alone).
    """
    if campaign_client is None or not campaign_id:
        return []
    try:
        dm_user_id = await campaign_client.campaign_dm(UUID(campaign_id))
    except (ValueError, TypeError) as exc:  # bad campaign id; never block generation
        logger.warning("could not resolve DM for campaign %s: %s", campaign_id, exc)
        return []
    if dm_user_id is None:
        return []
    target = str(dm_user_id)
    names: list[str] = []
    for label, info in speaker_map.items():
        if str(info.get("user_id") or "") != target:
            continue
        for key in ("character_name", "display_name"):
            if info.get(key):
                names.append(str(info[key]))
        if label in speaker_names:
            names.append(speaker_names[label])
    return sorted({n.strip() for n in names if n.strip()})


async def _build_chunk_views(
    event: Event,
    session_id: str,
    campaign_id: str,
    settings: ServiceSettings,
    storage: ObjectStorage,
    user_client: UserServiceClient,
    campaign_client: CampaignServiceClient | None,
) -> tuple[list[str], list[str], list[str]]:
    """Named transcript view ready for the LLM: (views, party, dm_names).

    Two sources, one output. With ATTRIBUTION_ENABLED the view comes from the
    ATTRIBUTED transcript (transcripts/{id}/attributed.json), which carries a
    per-utterance status and an [u_XXXXX] reference the extraction must echo back;
    without it, the old label map is used exactly as before, so the flag stays a
    real kill switch.
    """
    artifact = None
    if settings.attribution_enabled:
        artifact = await storage.read_json(
            settings.minio_transcripts_bucket,
            f"transcripts/{session_id}/attributed.json",
        )
    if artifact:
        return await _artifact_views(artifact, settings, storage)

    transcript = await storage.read_json(
        settings.minio_transcripts_bucket, f"transcripts/{session_id}/transcript.json"
    )
    speaker_map = build_speaker_map(event)
    speaker_names = await resolve_speaker_names(speaker_map, user_client)
    # The DM narrates the world but is not part of it: resolve the DM's
    # speaker from the campaign (best-effort) and keep its name out of the
    # party line, out of the prompt's character candidates and out of the
    # drafted pages. Falls back to the hardcoded narrator net in the merger.
    dm_names = await _dm_speaker_names(
        speaker_map, speaker_names, campaign_client, campaign_id
    )
    # Party CHARACTER names power the player-vs-NPC tagging and are shown
    # to the model so it never confuses a player with their character.
    # Raw casing is kept for the prompt; the merger normalizes for matching.
    party = [
        name
        for name in party_character_names(speaker_map)
        if name not in dm_names and not is_narrator_name(name)
    ]
    chunks = chunk_transcript(
        transcript.get("segments", []),
        speaker_names,
        max_tokens=settings.chunk_tokens,
        overlap=settings.chunk_overlap,
    )
    if not chunks:
        raise ValueError(f"session {session_id} transcript has no segments to generate from")
    if len(chunks) > settings.max_chunks_per_session:
        raise ValueError(
            f"session {session_id} yields {len(chunks)} chunks "
            f"(cap {settings.max_chunks_per_session}); refusing to generate"
        )
    party_note = (
        "Party (player characters): " + ", ".join(sorted(party)) + "\n\n"
        if party
        else ""
    )
    return [party_note + "\n".join(chunk) for chunk in chunks], party, dm_names


async def _artifact_views(
    artifact: dict[str, Any], settings: ServiceSettings, storage: ObjectStorage
) -> tuple[list[str], list[str], list[str]]:
    """The view built from the attributed transcript (the redesign's input).

    The roster in the artifact already resolved player/character/DM roles, so
    the party line and the out-of-world names come from it rather than from a
    label map: an utterance only carries a name when the engine is confident
    about it, and everything else is rendered '(unattributed)'.
    """
    roster = artifact.get("roster") or []
    party = [
        str(entry.get("character_name"))
        for entry in roster
        if entry.get("role") != "dm" and entry.get("character_name")
    ]
    dm_names = [
        str(entry.get("character_name") or entry.get("player_name") or "")
        for entry in roster
        if entry.get("role") == "dm"
    ]
    dm_names = [name for name in dm_names if name and not is_narrator_name(name)]
    chunks = chunk_artifact(
        artifact, max_tokens=settings.chunk_tokens, overlap=settings.chunk_overlap
    )
    if not chunks:
        raise ValueError("session has no attributed utterances to generate from")
    if len(chunks) > settings.max_chunks_per_session:
        raise ValueError(
            f"session yields {len(chunks)} chunks "
            f"(cap {settings.max_chunks_per_session}); refusing to generate"
        )
    party_note = (
        "Party (player characters): " + ", ".join(sorted(set(party))) + "\n\n"
        if party
        else ""
    )
    # The stretch note goes BETWEEN the party line and the lines it is about: it
    # is a reading of where this part of the session happens, and the extraction
    # has to see it next to the lines it licenses a name for (see
    # chunking.stretch_note).
    return [
        party_note + stretch_note(artifact, chunk) + "\n".join(chunk)
        for chunk in chunks
    ], party, dm_names


async def _load_attribution(
    session_id: str, settings: ServiceSettings, storage: ObjectStorage
) -> dict[str, Any] | None:
    """The attributed transcript, when the redesign is on and it exists."""
    if not settings.attribution_enabled:
        return None
    return await storage.read_json(
        settings.minio_transcripts_bucket, f"transcripts/{session_id}/attributed.json"
    )


def _artifact_player_names(artifact: dict[str, Any]) -> list[str]:
    """The PLAYERS' real names, which may never become character page titles."""
    return [
        str(entry.get("player_name"))
        for entry in artifact.get("roster") or []
        if entry.get("player_name")
    ]


async def _record_failure(
    db: AsyncSession,
    session_client: SessionServiceClient,
    session_id: str,
    job_id: UUID,
    exc: Exception,
) -> None:
    """Mark the job and the session failed (never raise from here)."""
    try:
        await job_services.fail_job(db, job_id, str(exc))
    except Exception:
        logger.exception("could not record job failure for session %s", session_id)
    try:
        await session_client.update_status(session_id, STATUS_FAILED, error=str(exc)[:2000])
    except Exception:
        logger.exception("could not mark session %s failed", session_id)


async def process_job(
    event: Event,
    settings: ServiceSettings,
    storage: ObjectStorage,
    session_client: SessionServiceClient,
    user_client: UserServiceClient,
    wiki_client: WikiServiceClient,
    llm: LLMClient,
    publisher: Callable[[Event], Awaitable[None]],
    db: AsyncSession,
    campaign_client: CampaignServiceClient | None = None,
) -> None:
    """PHASE 1: distill one session transcript into a DRAFT session summary.

    No wiki page, event page or timeline entry is created here — the summary
    is the reviewable intermediate layer. 'wiki_client' is part of the
    signature for symmetry with the other phases and is unused.
    """
    del wiki_client  # phase 1 writes nothing to the wiki
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))

    if event.type == "speakers.identified" and payload.get("pending_assignment"):
        logger.info(
            "session %s still has pending speaker assignments; waiting for speakers.assigned",
            session_id,
        )
        return

    # Late speakers.assigned events must not restart the summary of a session
    # the DM is already reviewing (or that already generated): only a session
    # that has not been distilled yet enters the summary phase.
    session = await session_client.get_session(session_id)
    status = str(session.get("status") or "")
    accepted = (
        ATTRIBUTION_SOURCE_STATUSES
        if settings.attribution_enabled
        else SUMMARY_SOURCE_STATUSES
    )
    if status not in accepted:
        logger.info(
            "session %s is '%s'; skipping %s (the summary phase only starts from %s)",
            session_id, status, event.type, " or ".join(sorted(accepted)),
        )
        return

    # Enter the phase; a 409 means another delivery already moved the session
    # (idempotency guard) -> nothing to do, ack and skip.
    try:
        await session_client.update_status(session_id, STATUS_SUMMARIZING)
    except ConflictTransition:
        logger.info("session %s already past %s; skipping", session_id, STATUS_SUMMARIZING)
        return

    job = await job_services.create_job(
        db,
        UUID(session_id),
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
        phase=PHASE_SUMMARY,
    )

    try:
        views, party, dm_names = await _build_chunk_views(
            event, session_id, campaign_id, settings, storage, user_client, campaign_client
        )
        artifact = await _load_attribution(session_id, settings, storage)
        extractions = await llm.extract_many(
            views,
            concurrency=settings.llm_chunk_concurrency,
            out_of_world=dm_names or None,
        )
        merged = merge_extractions(extractions)
        if dm_names:
            merged = exclude_character_names(merged, dm_names)
        if artifact is not None:
            # The gate: uncertainty decides what may reach a page. Enforced in
            # code, because a prompt is a request and this is the safety
            # property of the whole redesign (attribution-model S14.4).
            merged, gate_report = apply_gate(
                merged, artifact, player_names=_artifact_player_names(artifact)
            )
            logger.info("session %s: attribution gate %s", session_id, gate_report)
        composed = await _compose_summary(llm, merged, scenes=_scene_reading(artifact))
        if composed:
            merged["summary_blocks"] = composed
            merged["session_summary"] = blocks_to_text(composed)
        logger.info(
            "session %s: extraction language=%r (%d entities); summary %s",
            session_id, merged.get("language"),
            len(merged.get("characters", [])) + len(merged.get("locations", [])),
            describe_blocks(merged.get("summary_blocks") or merged.get("session_summary")),
        )
        summary = await job_services.save_summary(
            db,
            UUID(session_id),
            generation_job_id=job.id,
            merged=merged,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            prompt_version=settings.prompt_version,
            party_characters=party,
        )
        await job_services.complete_job(
            db, job.id, draft_ids=[], confidence=merged["confidence"]
        )
        await session_client.update_status(session_id, STATUS_SUMMARY_READY)
        await publisher(
            Event(
                type=SUMMARY_DRAFTED,
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "generation_job_id": str(job.id),
                    "summary_id": str(summary.id),
                    "revision": summary.revision,
                    "confidence": merged["confidence"],
                    "language": merged.get("language") or None,
                },
            )
        )
        logger.info(
            "session %s -> draft summary revision %s (confidence %s); awaiting DM review",
            session_id, summary.revision, merged["confidence"],
        )
    except Exception as exc:
        logger.exception("summary generation failed for session %s", session_id)
        await _record_failure(db, session_client, session_id, job.id, exc)
        raise


async def process_summary_regeneration(
    event: Event,
    settings: ServiceSettings,
    session_client: SessionServiceClient,
    llm: LLMClient,
    publisher: Callable[[Event], Awaitable[None]],
    db: AsyncSession,
) -> None:
    """PHASE 1b: rebuild the draft summary from the DM's review feedback."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))
    edits = [e for e in (payload.get("edits") or []) if isinstance(e, dict)]

    try:
        await session_client.update_status(session_id, STATUS_SUMMARIZING)
    except ConflictTransition:
        logger.info(
            "session %s is not awaiting a summary rewrite; skipping regeneration",
            session_id,
        )
        return

    job = await job_services.create_job(
        db,
        UUID(session_id),
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
        phase=PHASE_SUMMARY,
    )

    try:
        row = await job_services.latest_summary_for_session(db, UUID(session_id))
        if row is None:
            raise ValueError(f"session {session_id} has no summary to rewrite")
        current = job_services.summary_to_merged(row)
        patch = await llm.revise_summary(
            current,
            edits,
            summary_text_override=payload.get("summary_text") or None,
        )
        # The model returns a PATCH and this is where it is merged: the summary
        # it rewrote plus the items the correction touches. Everything else
        # keeps the value the DM was looking at, and a patch that does not fit
        # the extraction raises here (the job fails, nothing is persisted
        # half-applied) instead of being shrugged off.
        revised = apply_summary_revision(current, patch)
        requested_by = payload.get("requested_by")
        history = [
            {
                "targets": [
                    str(t) for t in (edit.get("targets") or []) if isinstance(t, str)
                ],
                "instruction": str(edit.get("instruction") or "").strip(),
                "requested_by": str(requested_by) if requested_by else None,
                "created_at": datetime.now(UTC).isoformat(),
            }
            for edit in edits
        ]
        updated = await job_services.apply_revision(
            db,
            UUID(session_id),
            generation_job_id=job.id,
            merged=revised,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            prompt_version=settings.prompt_version,
            edits=history,
        )
        await job_services.complete_job(
            db,
            job.id,
            draft_ids=[],
            confidence=float(updated.confidence) if updated.confidence is not None else 0.0,
        )
        await session_client.update_status(session_id, STATUS_SUMMARY_READY)
        await publisher(
            Event(
                type=SUMMARY_DRAFTED,
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "generation_job_id": str(job.id),
                    "summary_id": str(updated.id),
                    "revision": updated.revision,
                    "confidence": float(updated.confidence) if updated.confidence is not None else None,
                    "language": updated.language,
                },
            )
        )
        logger.info(
            "session %s -> summary revision %s (%d correction request(s)); %s",
            session_id, updated.revision, len(history),
            describe_revision(current, revised),
        )
    except Exception as exc:
        logger.exception("summary regeneration failed for session %s", session_id)
        await _record_failure(db, session_client, session_id, job.id, exc)
        raise


#: Below this many merged beats there is no story to write: one beat IS the
#: session, and the call would be a cost with nothing to buy.
MIN_SUMMARY_LINES_TO_COMPOSE = 2


def _scene_reading(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The record's "where this session happens", for the compose call.

    Only what a writer can use: the place of each stretch and who the reading
    puts there or elsewhere. It is the same reading the session page shows as
    the place labels of the narrative, so the story and the labels come from one
    source instead of two guesses.
    """
    if not artifact:
        return []
    scenes: list[dict[str, Any]] = []
    for stretch in artifact.get("stretches") or []:
        if not isinstance(stretch, dict):
            continue
        place = str(stretch.get("place") or "").strip()
        present = [str(n) for n in stretch.get("present") or []]
        absent = [str(n) for n in stretch.get("absent") or []]
        if not place and not present and not absent:
            continue
        scenes.append({"place": place, "present": present, "absent": absent})
    return scenes


async def _compose_summary(
    llm: Any, merged: dict[str, Any], scenes: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """Turn the merged per-chunk beats into the session's story.

    The extraction pass writes 1-3 beats per chunk and NEVER sees the other
    chunks, so what the merger produces is a list of separate moments with no
    thread - written by writers who could not know that the creature in one
    beat is the character named in another. This is the only call that sees the
    whole session: it answers with the narrative in scene blocks
    (app/summary.py), which is what the DM reads and highlights portions of.

    Never fatal: on any failure the beats stand as they are, and the DM reviews
    them as one flat story (app/summary.py turns them into blocks on read).
    """
    beats = summary_lines(str(merged.get("session_summary") or ""))
    if len(beats) < MIN_SUMMARY_LINES_TO_COMPOSE:
        return []
    try:
        composed = await llm.compose_summary(
            merged, language=merged.get("language"), scenes=scenes or None
        )
    except Exception:
        logger.exception("could not compose the session summary; keeping the beats")
        return []
    return composed or []


async def process_plan_generation(
    event: Event,
    settings: ServiceSettings,
    session_client: SessionServiceClient,
    wiki_client: WikiServiceClient,
    publisher: Callable[[Event], Awaitable[None]],
    db: AsyncSession,
) -> None:
    """PHASE 2: turn the CONFIRMED summary into a PROPOSED change set.

    Nothing is written to the wiki here. The proposed pages/updates/timeline
    entries are stored as a draft change set the DM reviews on the session
    page (the "git status" of the session), and the session parks on
    wiki_plan_ready until that review is confirmed.
    """
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))

    # Enter the planning phase; a 409 means another delivery already moved
    # the session (idempotency guard) -> ack and skip.
    try:
        await session_client.update_status(session_id, STATUS_GENERATING_WIKI)
    except ConflictTransition:
        logger.info(
            "session %s already past %s; skipping change-set generation",
            session_id, STATUS_GENERATING_WIKI,
        )
        return

    job = await job_services.create_job(
        db,
        UUID(session_id),
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
        phase=PHASE_WIKI,
    )

    try:
        # Generation starts ONLY from a summary the DM confirmed: without
        # that stamp the proposal would carry unreviewed text.
        summary = await job_services.confirmed_summary(db, UUID(session_id))
        if summary is None:
            raise ValueError(f"session {session_id} has no confirmed summary to expand")
        merged = job_services.summary_to_merged(summary)
        confirmed_by = payload.get("confirmed_by")
        # Existing campaign pages power cross-session dedupe: an entity the
        # wiki already documents is never proposed again. A listing failure
        # must not block the proposal - we just lose the dedupe net.
        try:
            existing_pages = await wiki_client.list_campaign_pages(campaign_id)
        except WikiServiceError as exc:
            logger.warning("could not list existing pages for %s: %s", campaign_id, exc)
            existing_pages = []
        change_set = build_change_set(
            merged,
            campaign_id,
            session_id,
            existing_pages=existing_pages,
            party_characters=list(summary.party_characters or []),
        )
        plan = await job_services.save_plan(
            db,
            UUID(session_id),
            summary_id=summary.id,
            generation_job_id=job.id,
            change_set=change_set,
            language=merged.get("language"),
        )
        confidence = merged.get("confidence")
        await job_services.complete_job(
            db,
            job.id,
            draft_ids=[],
            confidence=float(confidence) if confidence is not None else 0.0,
        )
        await session_client.update_status(session_id, STATUS_WIKI_PLAN_READY)
        counts = {
            "create": sum(1 for c in change_set["changes"] if c["action"] == "create"),
            "update": sum(1 for c in change_set["changes"] if c["action"] == "update"),
            "relations": len(change_set["relations"]),
            "skipped": len(change_set["skipped"]),
        }
        await publisher(
            Event(
                type=PLAN_READY,
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "generation_job_id": str(job.id),
                    "plan_id": str(plan.id),
                    "summary_id": str(summary.id),
                    "confirmed_by": str(confirmed_by) if confirmed_by else None,
                    **counts,
                },
            )
        )
        logger.info(
            "session %s -> proposed change set %s (%d new, %d updates, %d links); "
            "awaiting DM confirmation",
            session_id, plan.id, counts["create"], counts["update"], counts["relations"],
        )
    except Exception as exc:
        logger.exception("change-set generation failed for session %s", session_id)
        await _record_failure(db, session_client, session_id, job.id, exc)
        raise


def _apply_payload(plan: Any, campaign_id: str, session_id: str, confirmed_by: Any) -> dict:
    """The wiki-service payload of a confirmed change set (dropped items out)."""
    changes = [
        {
            "change_id": str(change.get("id")),
            "action": change.get("action") or "create",
            "kind": change.get("kind"),
            "title": (change.get("after") or {}).get("title") or change.get("title"),
            "page_id": change.get("page_id"),
            "content_json": (change.get("after") or {}).get("content_json") or {},
            "visibility": (change.get("after") or {}).get("visibility") or "public",
            "confidence": (change.get("after") or {}).get("confidence"),
            "timeline": change.get("timeline"),
        }
        for change in (plan.changes or [])
        if not change.get("dropped")
    ]
    relations = [
        {
            "relation_id": str(relation.get("id")),
            "from_title": relation.get("from_title"),
            "to_title": relation.get("to_title"),
            "to_page_id": relation.get("to_page_id"),
            "relation_type": relation.get("relation_type"),
        }
        for relation in (plan.relations or [])
        if not relation.get("dropped")
    ]
    return {
        "campaign_id": campaign_id,
        "session_id": session_id,
        "confirmed_by": str(confirmed_by) if confirmed_by else None,
        "changes": changes,
        "relations": relations,
    }


async def process_plan_application(
    event: Event,
    settings: ServiceSettings,
    session_client: SessionServiceClient,
    wiki_client: WikiServiceClient,
    publisher: Callable[[Event], Awaitable[None]],
    db: AsyncSession,
) -> None:
    """PHASE 3: write the CONFIRMED change set into the wiki.

    The pages are created published and the timeline entries approved
    (wiki-service internal apply endpoint): the DM confirmed the proposed
    changes, so nothing pipeline-generated ever lands in "pending review".
    """
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))

    # Enter the apply phase; a 409 means another delivery already moved the
    # session (idempotency guard) -> ack and skip.
    try:
        await session_client.update_status(session_id, STATUS_APPLYING_WIKI)
    except ConflictTransition:
        logger.info(
            "session %s already past %s; skipping change-set application",
            session_id, STATUS_APPLYING_WIKI,
        )
        return

    job = await job_services.create_job(
        db,
        UUID(session_id),
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
        phase=PHASE_APPLY,
    )

    try:
        plan = await job_services.get_plan(db, UUID(session_id))
        if plan is None:
            raise ValueError(f"session {session_id} has no proposed change set")
        if plan.status == PLAN_APPLIED:
            raise ValueError(f"the change set of session {session_id} is already applied")
        # The API stamps the confirmation when the DM clicks; stamping again
        # here keeps the row consistent for a redelivered message.
        if plan.confirmed_by is None:
            plan = await job_services.confirm_plan(
                db,
                UUID(session_id),
                confirmed_by=(
                    UUID(str(payload["confirmed_by"])) if payload.get("confirmed_by") else None
                ),
            )
        await job_services.mark_applying(db, UUID(session_id))
        confirmed_by = plan.confirmed_by or payload.get("confirmed_by")
        result = await wiki_client.apply_changes(
            _apply_payload(plan, campaign_id, session_id, confirmed_by)
        )
        created = result.get("created") or []
        updated = result.get("updated") or []
        skipped = result.get("skipped") or []
        confidences = [
            float((change.get("after") or {}).get("confidence"))
            for change in (plan.changes or [])
            if not change.get("dropped")
            and (change.get("after") or {}).get("confidence") is not None
        ]
        confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
        await job_services.complete_job(
            db,
            job.id,
            draft_ids=[str(item["page_id"]) for item in [*created, *updated] if item.get("page_id")],
            confidence=confidence,
        )
        await job_services.mark_applied(db, UUID(session_id), generation_job_id=job.id)
        await session_client.update_status(session_id, STATUS_CONTENT_READY)
        await publisher(
            Event(
                type="content.generated",
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "generation_job_id": str(job.id),
                    "summary_id": str(plan.summary_id) if plan.summary_id else None,
                    "plan_id": str(plan.id),
                    "draft_ids": [
                        str(item["page_id"]) for item in [*created, *updated] if item.get("page_id")
                    ],
                    "created": created,
                    "updated": updated,
                    "skipped": skipped,
                    "timeline_entries": result.get("timeline_entries") or 0,
                    "relations_created": result.get("relations_created") or 0,
                    "confidence": confidence,
                    "language": plan.language,
                },
            )
        )
        logger.info(
            "session %s -> change set applied: %d created, %d updated, %d skipped",
            session_id, len(created), len(updated), len(skipped),
        )
    except Exception as exc:
        logger.exception("change-set application failed for session %s", session_id)
        # the DM keeps the reviewed set: a retry re-confirms the same draft
        await job_services.mark_draft(db, UUID(session_id), error=str(exc)[:2000])
        await _record_failure(db, session_client, session_id, job.id, exc)
        raise


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: route the event to its pipeline phase."""
    settings = _get_settings()

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    async with session_factory(settings)() as db:
        if event.type == "summary.regenerate":
            await process_summary_regeneration(
                event, settings, get_session_client(settings), get_llm(settings),
                publisher, db,
            )
        elif event.type == "summary.confirmed":
            await process_plan_generation(
                event, settings, get_session_client(settings),
                get_wiki_client(settings), publisher, db,
            )
        elif event.type == "plan.confirmed":
            await process_plan_application(
                event, settings, get_session_client(settings),
                get_wiki_client(settings), publisher, db,
            )
        else:
            await process_job(
                event, settings, get_storage(settings), get_session_client(settings),
                get_user_client(settings), get_wiki_client(settings), get_llm(settings),
                publisher, db, campaign_client=get_campaign_client(settings),
            )


async def main() -> None:
    settings = _get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(
        connection,
        "content.generate",
        lambda event: handle(event, connection),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

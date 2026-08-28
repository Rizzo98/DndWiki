"""LLM wiki-draft generation worker.

Consumes content.generate (routing keys speakers.identified / speakers.assigned)
and produces structured wiki drafts (characters, locations, events, session
summary) via LiteLLM, creating pending_review pages through wiki-service.

Pipeline (per session):

1. Skip sessions that still have pending speaker assignments (a later
   speakers.assigned event re-triggers).
2. Move the session to 'generating_wiki' via the session-service internal API;
   a 409 (already generating / already done) means an earlier delivery handled
   it -> ack and skip (idempotency guard).
3. Download the named transcript from MinIO
   (transcripts/<session_id>/transcript.json), resolve speaker labels to player
   display names (user-service), and build a compact '[HH:MM:SS] NAME: text'
   view.
4. Split into overlapping token-bounded chunks; extract structured JSON from
   each chunk via LiteLLM (bounded concurrency).
5. Merge across chunks (dedupe by name, longest description, summed mentions)
   and map the result to CHARACTER/LOCATION wiki draft payloads; party
   members' character names tag player vs NPC pages. Cross-session dedupe:
   entities whose name/alias already exists as a campaign page are NOT
   re-drafted (their new facts stay visible on the persisted session
   summary); fuzzy matches still get a draft plus a 'possible_duplicate'
   relation proposal for the DM.
6. Create the draft pages (status=pending_review, confidence, source session),
   propose relations, record the generation_jobs row and publish
   content.generated (notification-service then tells the DM).

Failure semantics mirror transcription-service: failures mark the session
'failed' (recorded) and re-raise; the redelivered copy is acked as a no-op via
ConflictTransition, so a failed job is never re-run or DLQ-spammed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

import aio_pika
from dnd_common.db import session_factory
from dnd_common.events import Event, connect_rabbitmq, consume, publish
from sqlalchemy.ext.asyncio import AsyncSession

from app import services as job_services
from app.chunking import chunk_transcript
from app.clients.campaign_service import CampaignServiceClient
from app.clients.session_service import (
    STATUS_CONTENT_READY,
    STATUS_FAILED,
    STATUS_GENERATING_WIKI,
    ConflictTransition,
    SessionServiceClient,
)
from app.clients.user_service import UserServiceClient
from app.clients.wiki_service import WikiServiceClient, WikiServiceError
from app.core.config import ServiceSettings, get_settings
from app.extraction import LLMClient
from app.merger import (
    build_event_drafts,
    build_page_drafts,
    exclude_character_names,
    is_narrator_name,
    merge_extractions,
)
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

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


def _title_key(title: str) -> str:
    """Case/whitespace-insensitive page-title key for relation resolution."""
    return " ".join(title.lower().split())


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
    """Generate wiki drafts for one session end-to-end; raises on failure."""
    payload = event.payload
    session_id = str(payload["session_id"])
    campaign_id = str(payload.get("campaign_id", ""))

    if event.type == "speakers.identified" and payload.get("pending_assignment"):
        logger.info(
            "session %s still has pending speaker assignments; waiting for speakers.assigned",
            session_id,
        )
        return

    # Enter the pipeline; a 409 means another delivery already moved the
    # session (idempotency guard) -> nothing to do, ack and skip.
    try:
        await session_client.update_status(session_id, STATUS_GENERATING_WIKI)
    except ConflictTransition:
        logger.info("session %s already past %s; skipping", session_id, STATUS_GENERATING_WIKI)
        return

    job = await job_services.create_job(
        db,
        UUID(session_id),
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.prompt_version,
    )

    try:
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
        views = [party_note + "\n".join(chunk) for chunk in chunks]
        extractions = await llm.extract_many(
            views,
            concurrency=settings.llm_chunk_concurrency,
            out_of_world=dm_names or None,
        )
        merged = merge_extractions(extractions)
        if dm_names:
            merged = exclude_character_names(merged, dm_names)
        logger.info(
            "session %s: extraction language=%r (%d entities)",
            session_id, merged.get("language"),
            len(merged.get("characters", [])) + len(merged.get("locations", [])),
        )
        # Persist the merged summary first, so the session page can show it even
        # if wiki draft creation fails (wiki-service down, etc.).
        await job_services.save_summary(
            db,
            UUID(session_id),
            generation_job_id=job.id,
            merged=merged,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            prompt_version=settings.prompt_version,
        )
        # Existing campaign pages power cross-session dedupe: an entity the
        # wiki already documents is not drafted again. A listing failure must
        # never block generation - we just lose the dedupe net.
        try:
            existing_pages = await wiki_client.list_campaign_pages(campaign_id)
        except WikiServiceError as exc:
            logger.warning("could not list existing pages for %s: %s", campaign_id, exc)
            existing_pages = []
        drafts, relations, duplicates = build_page_drafts(
            merged,
            campaign_id,
            session_id,
            existing_pages=existing_pages,
            party_characters=list(party),
        )
        event_drafts, event_updates, timeline_events, event_duplicates = (
            build_event_drafts(
                merged, campaign_id, session_id, existing_pages=existing_pages
            )
        )

        created: list[dict] = []
        for draft in drafts:
            created.append(await wiki_client.create_page(draft))
        for draft in event_drafts:
            created.append(await wiki_client.create_page(draft))

        # Propose relations: 'possible_duplicate' points straight at an
        # existing page; auto-extracted durable relationships ('member_of',
        # 'allied_with', 'led_by') resolve the target by name through the
        # just-created drafts first, then through already-existing pages
        # (matched by title or alias).
        title_to_id = {
            _title_key(page["title"]): str(page["id"]) for page in created
        }
        existing_title_to_id: dict[str, str] = {}
        for page in existing_pages:
            for name in [page.get("title"), *(page.get("aliases") or [])]:
                key = _title_key(name or "")
                if key:
                    existing_title_to_id.setdefault(key, str(page["id"]))
        for rel in relations:
            from_id = title_to_id.get(_title_key(rel["from_title"]))
            to_key = _title_key(rel.get("to_title") or "")
            to_id = (
                rel.get("to_page_id")
                or title_to_id.get(to_key)
                or existing_title_to_id.get(to_key)
            )
            if from_id and to_id and from_id != to_id:
                try:
                    await wiki_client.create_relation(from_id, to_id, rel["relation_type"])
                except WikiServiceError as exc:
                    logger.warning(
                        "could not propose relation %s -> %s: %s",
                        rel["from_title"], rel.get("to_title") or rel.get("to_page_id"), exc,
                    )
        for duplicate in duplicates + event_duplicates:
            logger.info(
                "session %s: '%s' matches existing page %s (%s); skipped - the "
                "new facts stay on the session summary",
                session_id, duplicate["title"],
                duplicate.get("matched_title"), duplicate.get("matched_page_id"),
            )

        # World events already on the timeline are updated, not duplicated:
        # merge the new information into the existing event page.
        for update in event_updates:
            try:
                await wiki_client.update_page(
                    update["page_id"],
                    {
                        "content_json": update["content_json"],
                        "change_note": update["change_note"],
                    },
                )
                logger.info(
                    "session %s: event page %s (%s) updated with new information",
                    session_id, update["page_id"], update["title"],
                )
            except WikiServiceError as exc:
                logger.warning(
                    "could not update event page %s (%s): %s",
                    update["page_id"], update["title"], exc,
                )

        # Every event (new or updated) backs a campaign timeline entry; the
        # entry is created as pending (approved=False) or refreshed if the
        # page already has one. New drafts resolve their page id by title.
        for entry in timeline_events:
            page_id = entry.get("page_id") or title_to_id.get(_title_key(entry["title"]))
            if not page_id:
                logger.warning(
                    "session %s: no page id for timeline event '%s'; skipping",
                    session_id, entry["title"],
                )
                continue
            try:
                await wiki_client.upsert_timeline_event(
                    {
                        "campaign_id": campaign_id,
                        "page_id": page_id,
                        "in_world_date": entry.get("in_world_date"),
                        "summary": entry["summary"],
                        "source_session_id": entry.get("source_session_id"),
                    }
                )
            except WikiServiceError as exc:
                logger.warning(
                    "could not upsert timeline entry for '%s': %s",
                    entry["title"], exc,
                )

        await job_services.complete_job(
            db,
            job.id,
            draft_ids=[str(page["id"]) for page in created],
            confidence=merged["confidence"],
        )
        await session_client.update_status(session_id, STATUS_CONTENT_READY)
        await publisher(
            Event(
                type="content.generated",
                payload={
                    "session_id": session_id,
                    "campaign_id": campaign_id,
                    "generation_job_id": str(job.id),
                    "draft_ids": [str(page["id"]) for page in created],
                    "confidence": merged["confidence"],
                    "language": merged.get("language") or None,
                    "skipped_duplicates": duplicates + event_duplicates,
                },
            )
        )
        logger.info(
            "session %s -> %d drafts (confidence %s)",
            session_id, len(created), merged["confidence"],
        )
    except Exception as exc:
        logger.exception("content generation failed for session %s", session_id)
        try:
            await job_services.fail_job(db, job.id, str(exc))
        except Exception:
            logger.exception("could not record job failure for session %s", session_id)
        try:
            await session_client.update_status(session_id, STATUS_FAILED, error=str(exc)[:2000])
        except Exception:
            logger.exception("could not mark session %s failed", session_id)
        raise


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: wire process_job with process-level singletons."""
    settings = _get_settings()
    storage = get_storage(settings)
    session_client = get_session_client(settings)
    user_client = get_user_client(settings)
    wiki_client = get_wiki_client(settings)
    campaign_client = get_campaign_client(settings)
    llm = get_llm(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    async with session_factory(settings)() as db:
        await process_job(
            event, settings, storage, session_client, user_client, wiki_client,
            llm, publisher, db, campaign_client=campaign_client,
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
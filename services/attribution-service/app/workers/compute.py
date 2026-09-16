"""The attribution worker: consumes attribution.jobs and runs the pass.

Queue bindings (docs/attribution-model.md S15.2):

  speakers.identified        -> the engine takes over from speaker-service
  attribution.answered       -> a DM answered; recompute and propagate
  attribution.recompute      -> an explicit admin/campaign-level recompute

The worker owns ALL the IO (MinIO, Postgres, HTTP) and calls the pure pass in
app.pipeline. Everything the pass needs is assembled here, which is what keeps
the engine testable without a single container.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from dnd_common.db import session_factory
from dnd_common.events import Event, connect_rabbitmq, consume, publish
from sqlalchemy.ext.asyncio import AsyncSession

from app.calibration import Calibration
from app.capabilities import CapabilityStore, from_wiki_characters, name_key
from app.clients import (
    STATUS_ATTRIBUTING,
    STATUS_ATTRIBUTION_READY,
    STATUS_ATTRIBUTION_REVIEW,
    STATUS_FAILED,
    CampaignServiceClient,
    ConflictTransition,
    ServiceError,
    SessionServiceClient,
    SpeakerServiceClient,
    WikiServiceClient,
)
from app.core.config import ServiceSettings, get_settings
from app.evidence import EvidencePass
from app.evidence_llm import EvidenceLLM, run_evidence_pass, run_scene_pass
from app.pipeline import ObservationInput, PlanInput, plan_session
from app.review import ReviewPolicy
from app.services import review as review_service
from app.statuses import StatusThresholds
from app.storage import ObjectStorage
from app.utterances import Utterance, build_utterances, gap_before

logger = logging.getLogger(__name__)


def review_event(
    *,
    session_id: str,
    campaign_id: str,
    coverage: float,
    unresolved: int,
    questions_planned: int,
    run_id: str,
) -> Event:
    """The event that moves the session on after an attribution pass.

    Two outcomes, one meaning: either there are questions for the DM, or there
    is nothing left worth asking. The second one still has to be ANNOUNCED. A
    pass with no questions parks the session on 'attribution_ready', and no
    other event can leave that status - so it sat there until the DM happened
    to open the page and press Finish on a review that had no question in it.
    """
    common = {
        "campaign_id": campaign_id,
        "session_id": session_id,
        "coverage": round(coverage, 4),
        "unresolved": unresolved,
    }
    if questions_planned:
        return Event(
            type="attribution.review.ready",
            payload={**common, "questions_planned": questions_planned},
        )
    return Event(
        type="attribution.review.completed",
        payload={
            **common,
            "outcome": "nothing_to_ask",
            "questions_asked": 0,
            "review_run_id": run_id,
        },
    )


def character_members(members: list[dict[str, Any]]) -> dict[str, str]:
    """Normalised character name -> member id, for the wiki join.

    Player names are included as well: a DM occasionally titles a page after the
    player rather than the character, and a page that resolves to the wrong side
    of the same person is still that person.
    """
    out: dict[str, str] = {}
    for member in members:
        member_id = str(member.get("member_id") or member.get("id") or "").strip()
        if not member_id:
            continue
        for field in ("character_name", "player_name"):
            key = name_key(member.get(field))
            if key:
                out.setdefault(key, member_id)
    return out


_settings: ServiceSettings | None = None
_storage: ObjectStorage | None = None


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


def thresholds_of(settings: ServiceSettings) -> StatusThresholds:
    return StatusThresholds(
        auto_high_pmin=settings.auto_high_pmin,
        auto_high_margin=settings.auto_high_margin,
        auto_high_min_channel_lr=settings.auto_high_min_channel_lr,
        auto_high_min_voice_lr=settings.auto_high_min_voice_lr,
        auto_low_pmin=settings.auto_low_pmin,
        propagated_pmin=settings.propagated_pmin,
        propagated_margin=settings.propagated_margin,
    )


def policy_of(settings: ServiceSettings) -> ReviewPolicy:
    return ReviewPolicy(
        gain_floor_bits=settings.gain_floor_bits,
        target_unresolved=settings.target_unresolved,
        max_questions=settings.max_questions,
        event_stakes_threshold=settings.event_stakes_threshold,
        max_consecutive_idk=settings.max_consecutive_idk,
        uninformative_penalty=settings.uninformative_penalty,
        overlap_penalty=settings.overlap_penalty,
    )


# --- assembling the pass ----------------------------------------------------


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _candidate_key(hit: dict[str, Any]) -> str | None:
    """'member:<id>' for the new prints, 'user:<id>' for the legacy ones."""
    member_id = hit.get("member_id")
    if member_id:
        return f"member:{member_id}"
    user_id = hit.get("user_id")
    if user_id:
        return f"user:{user_id}"
    return None


def user_to_member(members: list[dict[str, Any]]) -> dict[str, str]:
    """user id -> member id, so a legacy user-keyed print still resolves."""
    return {
        str(member["user_id"]): str(member.get("member_id") or member.get("id") or "")
        for member in members
        if member.get("user_id") and (member.get("member_id") or member.get("id"))
    }


def observations_from_records(
    records: list[dict[str, Any]], utterances: list[Utterance]
) -> dict[str, ObservationInput]:
    """Attach the per-turn voice evidence to the utterances it covers.

    speaker-service holds one observation per SPEAKER TURN; the engine works per
    UTTERANCE, and a long turn can hold several. Each utterance therefore takes
    the observation whose span contains it, and an utterance outside every
    observation (a turn too short to embed, or a session identified before the
    observation store existed) gets none - which is honest rather than lossy: it
    has no voice evidence and the other channels decide it alone.
    """
    out: dict[str, ObservationInput] = {}
    for utterance in utterances:
        for record in records:
            start = float(record.get("start") or 0.0)
            end = float(record.get("end") or 0.0)
            if start - 0.05 <= utterance.start and utterance.end <= end + 0.05:
                nearest = (
                    (_candidate_key(hit), float(hit.get("cosine") or 0.0))
                    for hit in (record.get("nearest_centroids") or [])
                )
                out[utterance.ref] = ObservationInput(
                    ref=str(record.get("observation_id") or ""),
                    label=record.get("label"),
                    quality=_float(record.get("quality")),
                    vector=tuple(float(x) for x in (record.get("vector") or ())),
                    nearest=tuple(
                        (key, cosine) for key, cosine in nearest if key
                    ),
                )
                break
    return out


# --- the pass ---------------------------------------------------------------


async def compute_session(
    db: AsyncSession,
    *,
    session_id: Any,
    campaign_id: str | None,
    settings: ServiceSettings,
    revision: int | None = None,
    storage: ObjectStorage | None = None,
    members: list[dict[str, Any]] | None = None,
    characters: list[dict[str, Any]] | None = None,
    evidence: EvidencePass | None = None,
    observation_records: list[dict[str, Any]] | None = None,
    publisher: Callable[[Event], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Run the attribution pass for one session and persist the result.

    The order matters and is not arbitrary:

    1. build the utterances, because everything else is keyed by their refs;
    2. attach the per-turn voice embeddings from speaker-service (the vectors,
       not just the cosines: without them there is no purity test, no
       utterance-level defection and no similarity propagation);
    3. run the identity-evidence pass over the REAL references, so every claim it
       returns points at an utterance that exists;
    4. plan.

    Steps 2 and 3 both degrade rather than fail: a missing observation store or
    an unavailable LLM leaves the engine running on what it has, with a lower and
    honest coverage (docs/attribution-model.md S16).
    """
    storage = storage or get_storage(settings)
    session_uuid = session_id

    diarization = await storage.read_json(f"transcripts/{session_uuid}/diarization.json")
    segments = (diarization or {}).get("segments") or []
    if not segments:
        logger.info("session %s has no diarized segments; nothing to attribute", session_uuid)
        return {"session_id": str(session_uuid), "computed": False, "reason": "no_segments"}

    members = members if members is not None else []
    capabilities = CapabilityStore()
    # The wiki page says "Thorin is a Fighter"; the roster says Thorin is
    # member <id>. Only the two together narrow the candidate set, so the join
    # happens here where both are in hand.
    for capability in from_wiki_characters(
        characters or [], member_of=character_members(members)
    ):
        capabilities.add(capability)
    calibration = await _calibration(db, campaign_id, settings)

    # 1. utterances
    utterances = gap_before(build_utterances(segments).utterances)
    if not utterances:
        logger.info("session %s has no attributable utterances", session_uuid)
        return {"session_id": str(session_uuid), "computed": False, "reason": "no_utterances"}

    # 2. voice evidence (vectors + cosines), best effort
    records = observation_records
    if records is None:
        records = await _fetch_observations(str(session_uuid), settings)
    observations = observations_from_records(records, utterances)

    # 3. the identity-evidence pass, best effort
    pass_ = evidence
    if pass_ is None:
        pass_ = await _run_evidence(
            utterances, members, capabilities, settings
        )

    # 3b. the scene reading (S12.6), best effort and AFTER the pass above: it
    # reads the gists that pass produced, from the whole session at once, and it
    # is what tells the engine who the transcript places in which stretch - the
    # absence evidence and the presence questions both come from here.
    if not pass_.scenes:
        scenes_at = time.perf_counter()
        scene_map = await run_scene_pass(
            utterances=utterances,
            roster=_roster_lines(members, capabilities),
            roster_names=_scene_names(members),
            settings=settings,
        )
        pass_.scenes = scene_map.scenes
        logger.info(
            "session %s scenes read in %.1fs", session_uuid, time.perf_counter() - scenes_at
        )

    plan_input = PlanInput(
        session_id=str(session_uuid),
        campaign_id=str(campaign_id or ""),
        language=str((diarization or {}).get("language") or "en"),
        segments=segments,
        utterances=utterances,
        members=members,
        observations=observations,
        user_to_member=user_to_member(members),
        evidence=pass_,
        calibration=calibration,
        capabilities=capabilities,
        thresholds=thresholds_of(settings),
        policy=policy_of(settings),
        # The engine's own knobs, wired rather than left at their defaults: a
        # threshold that cannot be reached from the environment is a policy the
        # operator cannot change (S15.5).
        purity_split_threshold=settings.purity_split_threshold,
        same_voice_neighbours=settings.same_voice_neighbours,
        same_voice_eps=settings.same_voice_eps,
        engine_version=settings.engine_version,
    )
    planned_at = time.perf_counter()
    plan = plan_session(plan_input)
    logger.info(
        "session %s planned in %.1fs: %d utterances, %d covered by a voice "
        "observation (%d of them actually MATCHED an enrolled voice), "
        "%d evidence items, %d candidate question(s), coverage %.3f",
        session_uuid,
        time.perf_counter() - planned_at,
        len(utterances),
        len(observations),
        sum(1 for o in observations.values() if o.nearest),
        len(pass_.items),
        len(plan.questions),
        plan.coverage,
    )

    next_revision = revision or await _next_revision(db, session_uuid)
    # The greedy simulation is the most expensive thing here, and it produces
    # the number the DM is shown ("answer about N questions"). Timed and logged
    # because when it goes wrong it does not fail - it just takes hours, and a
    # worker with no progress line looks identical to a worker that is stuck.
    plan_at = time.perf_counter()
    planned = plan.review.plan().planned
    logger.info(
        "session %s: greedy review plan = %d question(s) in %.1fs "
        "(budget %d simulated candidates x %d options)",
        session_uuid,
        planned,
        time.perf_counter() - plan_at,
        settings.simulation_budget,
        settings.max_simulated_options,
    )

    stored_at = time.perf_counter()
    run = await review_service.store_plan(
        db,
        plan,
        revision=next_revision,
        thresholds=plan_input.thresholds,
        policy=plan_input.policy,
        questions_planned=planned,
    )
    logger.info(
        "session %s: stored revision %d in %.1fs",
        session_uuid,
        next_revision,
        time.perf_counter() - stored_at,
    )

    artifact = plan.attributed_transcript(revision=next_revision)
    await storage.put_json(f"transcripts/{session_uuid}/attributed.json", artifact)

    if publisher is not None:
        await publisher(
            Event(
                type="attribution.computed",
                payload={
                    "session_id": str(session_uuid),
                    "campaign_id": str(campaign_id or ""),
                    "revision": next_revision,
                    "coverage": round(plan.coverage, 4),
                    "unresolved": sum(
                        1 for v in plan.verdicts.values() if v.status == "unresolved"
                    ),
                    "questions_planned": run.questions_planned,
                    "questions_asked": run.questions_asked,
                    "engine_version": settings.engine_version,
                    "converged": plan.converged,
                },
            )
        )
        await publisher(
            review_event(
                session_id=str(session_uuid),
                campaign_id=str(campaign_id or ""),
                coverage=plan.coverage,
                unresolved=sum(
                    1 for v in plan.verdicts.values() if v.status == "unresolved"
                ),
                questions_planned=run.questions_planned,
                run_id=str(run.id),
            )
        )
    return {
        "session_id": str(session_uuid),
        "computed": True,
        "revision": next_revision,
        "coverage": round(plan.coverage, 4),
        "questions_planned": run.questions_planned,
        "converged": plan.converged,
        "method": plan.method,
        "buckets": plan.buckets(),
    }


async def _fetch_observations(
    session_id: str, settings: ServiceSettings
) -> list[dict[str, Any]]:
    """The per-turn embeddings speaker-service stored for this session.

    Never raises. The client already turns an HTTP failure into an empty list,
    but this is the worker's own boundary: a session must not fail because an
    OPTIONAL input was unavailable, and relying on the client to guarantee that
    would make any future client change a silent outage.
    """
    try:
        return await SpeakerServiceClient(settings).voice_observations(session_id)
    except Exception:  # degrade to label granularity
        logger.exception(
            "could not read the voice observations of session %s; "
            "attributing at label granularity",
            session_id,
        )
        return []


async def _run_evidence(
    utterances: list[Utterance],
    members: list[dict[str, Any]],
    capabilities: CapabilityStore,
    settings: ServiceSettings,
) -> EvidencePass:
    """Render the session and run the identity-evidence pass over it.

    The voice marker shown to the model is the DIARIZATION LABEL: at this point
    the engine has not grouped observations into identities yet, and the label is
    itself an anonymous voice group - which is exactly what the prompt says the
    marker means. Member names are shown only where a voice model already places
    the label, so the pass gets anchors without ever being asked to produce one.
    """
    return await run_evidence_pass(
        utterances=utterances,
        voice_of={u.ref: (u.diar_label or "-") for u in utterances},
        member_of={},
        roster=_roster_lines(members, capabilities),
        settings=settings,
        llm=EvidenceLLM(settings),
    )


def _scene_names(members: list[dict[str, Any]]) -> dict[str, str]:
    """Lowercased name -> the roster's own spelling, for the scene reading.

    Both the character and the player name map to the CHARACTER: a table says
    "Bob is running Keth" as often as it says "Keth", and a scene's cast is about
    characters. A name the model returns that is not in this map is dropped
    (app.scenes.parse_scenes): the engine keys candidates by member, and a name
    that matches nobody is worse than no name.
    """
    names: dict[str, str] = {}
    for member in members:
        character = str(member.get("character_name") or "").strip()
        if not character:
            continue
        names[character.lower()] = character
        player = str(member.get("player_name") or "").strip()
        if player:
            names.setdefault(player.lower(), character)
    return names


def _roster_lines(members: list[dict[str, Any]], capabilities: CapabilityStore) -> list[str]:
    """The roster header of the evidence view, with what each member can do."""
    from app.evidence import roster_block

    by_member = {
        member_id: sorted(values) for member_id, values in capabilities.by_member().items()
    }
    normalized = []
    for member in members:
        entry = dict(member)
        if not entry.get("member_id") and entry.get("id"):
            entry["member_id"] = entry["id"]
        normalized.append(entry)
    return roster_block(normalized, by_member)


async def load_artifact(session_id: Any) -> dict[str, Any] | None:
    settings = _get_settings()
    return await get_storage(settings).read_json(
        f"transcripts/{session_id}/attributed.json"
    )


async def _next_revision(db: AsyncSession, session_id: Any) -> int:
    from app.models import SessionBeliefStats

    stats = await db.get(SessionBeliefStats, session_id)
    return (stats.revision + 1) if stats is not None else 1


async def _calibration(
    db: AsyncSession, campaign_id: str | None, settings: ServiceSettings
) -> Calibration:
    """The campaign's fitted calibration, or the cold-start default.

    Fitting needs labelled pair similarities; the engine asks session-service for
    the campaign's history and derives them from the CONFIRMED labels only. With
    too few labels it returns the cold-start ramp rather than a badly fitted
    curve (S7.2).
    """
    if not campaign_id:
        return Calibration()

    from sqlalchemy import select

    from app.models import AttributionCalibration

    row = await db.scalar(
        select(AttributionCalibration).where(
            AttributionCalibration.campaign_id == _maybe_uuid(campaign_id),
            AttributionCalibration.model_version == settings.engine_version,
        )
    )
    if row is not None and row.params:
        return Calibration.from_params(row.params)
    return Calibration()


def _maybe_uuid(value: str):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# --- queue ------------------------------------------------------------------


async def handle(event: Event, connection: aio_pika.abc.AbstractConnection) -> None:
    """Consume handler: run the pass for the session the event names."""
    settings = _get_settings()
    if not settings.attribution_enabled:
        logger.info(
            "attribution disabled (ATTRIBUTION_ENABLED=false); ignoring %s", event.type
        )
        return

    payload = event.payload
    session_id = str(payload.get("session_id") or "")
    campaign_id = str(payload.get("campaign_id") or "")
    if not session_id:
        logger.warning("%s carries no session_id; ignoring", event.type)
        return

    client = SessionServiceClient(settings)
    campaign_client = CampaignServiceClient(settings)
    wiki_client = WikiServiceClient(settings)

    async def publisher(ev: Event) -> None:
        await publish(connection, ev)

    try:
        await client.update_status(session_id, STATUS_ATTRIBUTING)
    except ConflictTransition:
        logger.info("session %s already moved past attributing; skipping", session_id)
        return
    except ServiceError:
        logger.warning("could not move %s to attributing; continuing", session_id)

    factory = session_factory(settings)
    try:
        members = await campaign_client.list_members(campaign_id) if campaign_id else []
        characters = (
            await wiki_client.character_pages(campaign_id) if campaign_id else []
        )
        async with factory() as db:
            result = await compute_session(
                db,
                session_id=_maybe_uuid(session_id),
                campaign_id=campaign_id,
                settings=settings,
                members=members,
                characters=characters,
                publisher=publisher,
            )
        logger.info(
            "session %s: %d cast member(s), %d character page(s) mined for capabilities",
            session_id,
            len(members),
            len(characters),
        )
        await client.update_status(
            session_id,
            STATUS_ATTRIBUTION_REVIEW if result.get("questions_planned") else STATUS_ATTRIBUTION_READY,
        )
        logger.info(
            "session %s attribution done: revision %s, %s question(s) planned, "
            "coverage %s, method %s, converged %s",
            session_id,
            result.get("revision"),
            result.get("questions_planned"),
            result.get("coverage"),
            result.get("method"),
            result.get("converged"),
        )
    except Exception as exc:
        logger.exception("attribution failed for session %s", session_id)
        try:
            await client.update_status(session_id, STATUS_FAILED, error=str(exc)[:2000])
        except Exception:
            logger.exception("could not mark session %s failed", session_id)
        raise


async def main() -> None:
    settings = _get_settings()
    connection = await connect_rabbitmq(settings.rabbitmq_url)
    await consume(
        connection,
        "attribution.jobs",
        lambda event: handle(event, connection),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import asyncio

    asyncio.run(main())

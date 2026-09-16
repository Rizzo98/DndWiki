"""The review API behind the gateway (docs/attribution-model.md S15.4).

Every answer is a GLOBAL update: POST /answer runs the propagation, re-classifies
the session, records the propagation event and returns what changed - including
the count of other moments the answer resolved, which is the number the DM
actually cares about.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from dnd_common.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import ServiceSettings, get_settings
from app.deps import require_session_member
from app.models import (
    PropagationEvent,
    ReviewRun,
    SessionBeliefStats,
    SessionScene,
    Utterance,
    UtteranceAttribution,
    VoiceIdentity,
)
from app.review import ReviewPolicy
from app.services import review as review_service
from app.statuses import StatusThresholds

# The gateway sends everything under /api/attribution here (docker-compose
# labels), so the public prefix must carry it. Without it the DM's browser hits
# /api/sessions/{id}/review on session-service and gets a plain 404.
#
# require_session_member is ROUTER-WIDE: every route below reads a session's
# transcript and can write its attribution, so authentication and membership are
# the same rule for all of them. The gateway is not an auth boundary - each
# service validates the caller's token itself.
router = APIRouter(
    prefix="/api/attribution/sessions/{session_id}",
    tags=["attribution"],
    dependencies=[Depends(require_session_member)],
)


def _policy(settings: ServiceSettings) -> ReviewPolicy:
    return ReviewPolicy(
        gain_floor_bits=settings.gain_floor_bits,
        target_unresolved=settings.target_unresolved,
        max_questions=settings.max_questions,
        event_stakes_threshold=settings.event_stakes_threshold,
        max_consecutive_idk=settings.max_consecutive_idk,
        uninformative_penalty=settings.uninformative_penalty,
        overlap_penalty=settings.overlap_penalty,
        simulation_budget=settings.simulation_budget,
        max_simulated_options=settings.max_simulated_options,
    )


def _thresholds(settings: ServiceSettings) -> StatusThresholds:
    return StatusThresholds(
        auto_high_pmin=settings.auto_high_pmin,
        auto_high_margin=settings.auto_high_margin,
        auto_high_min_channel_lr=settings.auto_high_min_channel_lr,
        auto_high_min_voice_lr=settings.auto_high_min_voice_lr,
        auto_low_pmin=settings.auto_low_pmin,
        propagated_pmin=settings.propagated_pmin,
        propagated_margin=settings.propagated_margin,
    )


def _session_uuid(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown session"
        ) from exc


async def _review_or_404(
    db: AsyncSession, session_id: uuid.UUID, settings: ServiceSettings
):
    session, stats = await review_service.load_review(
        db,
        session_id,
        policy=_policy(settings),
        thresholds=_thresholds(settings),
    )
    if session is None or stats is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this session has no attribution yet",
        )
    return session, stats


@router.get("/review")
async def review_status_view(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Coverage, buckets, the question counter and the run's status."""
    session_id_uuid = _session_uuid(session_id)
    session, stats = await _review_or_404(db, session_id_uuid, settings)
    run = await db.scalar(select(ReviewRun).where(ReviewRun.session_id == session_id_uuid))
    return review_payload(session, stats, run, max_questions=settings.max_questions)


def review_payload(
    session: Any, stats: Any, run: Any, *, max_questions: int
) -> dict[str, Any]:
    """The body of GET /review.

    A function rather than a route body, because a route body is a place bugs
    hide: this one lost its `return` in an edit and nothing noticed, since no
    test could reach it without a database. Everything below is pure.
    """
    # The stored count, NOT a fresh greedy simulation: the number was computed
    # when the belief was written, and recomputing it here made every page load
    # pay the most expensive computation in the system.
    payload = session.status(questions_planned=run.questions_planned if run else None)
    payload["run"] = {
        "status": run.status if run else "pending",
        "questions_planned": run.questions_planned if run else None,
        "questions_asked": run.questions_asked if run else 0,
        "coverage_before": (
            float(run.coverage_before)
            if run and run.coverage_before is not None
            else None
        ),
        "coverage_after": (
            float(run.coverage_after)
            if run and run.coverage_after is not None
            else None
        ),
        "stop_reason": stats.stop_reason,
        "engine_version": stats.engine_version,
    }
    payload["coverage"] = round(session._coverage(), 4)
    payload["unresolved"] = round(session.unresolved(), 4)
    payload["plan"] = plan_progress(run, max_questions=max_questions)
    return payload


def plan_progress(run: Any, *, max_questions: int) -> dict[str, Any]:
    """How far the DM is through the questions, in numbers that stay TRUE.

    Two numbers, and both are FACTS: how many questions they have answered, and
    how many one review asks. What is deliberately NOT here is any "N questions
    to finish". The length of a review is not knowable in advance - it is a
    function of the answers, because every answer moves the belief and the
    stopping rule reads the state that produces.

    The greedy simulation that used to be shown here is a BEST CASE: it assumes
    the DM answers every question the way the evidence points. On a real session
    it said 6; the DM answered 8; the session still had 84% of what matters
    unattributed; re-run on the state those answers produced, the same
    simulation says 8 - the entire budget. So the DM was shown "about 6
    questions to finish" and then "8 of 6 answered", and both numbers were
    artefacts of asking a question that has no answer (S9.4). The stored count
    is still computed and returned in the run block, as a diagnostic.
    """
    answered = (getattr(run, "questions_asked", 0) or 0) if run is not None else 0
    return {
        "answered": answered,
        "max_questions": max_questions,
        "budget_spent": answered >= max_questions,
    }


@router.get("/review/next-question")
async def next_question(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """The top-ranked question, or null plus the reason the review is over."""
    session, _ = await _review_or_404(db, _session_uuid(session_id), settings)
    scored, decision = session.next_question()
    if scored is None:
        return {"question": None, "stop": decision.as_payload()}
    return {
        "question": {
            "id": None,
            **scored.question.as_payload(),
            "expected_gain": round(scored.expected_gain, 6),
            "score": round(scored.score, 6),
            "gain_detail": scored.as_payload()["gain_detail"],
        },
        "stop": decision.as_payload(),
    }


@router.post("/review/answer")
async def answer_question(
    session_id: str,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Answer one question; returns what else it resolved."""
    session_id_uuid = _session_uuid(session_id)
    session, stats = await _review_or_404(db, session_id_uuid, settings)
    prompt = str(body.get("question") or body.get("prompt_text") or "")
    option = str(body.get("option") or "")
    if not prompt or not option:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="question and option are required",
        )
    scored = next(
        (s for s in session.ranked() if s.question.prompt_text == prompt), None
    )
    if scored is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that question is no longer asked (the belief moved on)",
        )

    outcome = session.answer(scored, option)
    stats.belief = _snapshot_with(session, stats.belief)
    stats.coverage = session._coverage()
    stats.unresolved_stakes = session.unresolved()
    db.add(
        PropagationEvent(
            session_id=session_id_uuid,
            question_id=None,
            resolved_utterances=len(outcome.resolved),
            resolved_sec=outcome.resolved_seconds,
            voice_ids=[
                uuid.uuid5(uuid.NAMESPACE_URL, f"dnd:voice:{session_id_uuid}:{vid}")
                for vid in outcome.voice_ids
            ],
            learned={
                "capabilities": list(outcome.learned_capabilities),
                "voice_centroids": outcome.learned_voice_models,
            },
        )
    )
    run = await db.scalar(select(ReviewRun).where(ReviewRun.session_id == session_id_uuid))
    if run is not None:
        run.questions_asked = (run.questions_asked or 0) + 1
        run.status = "in_progress"
    await db.commit()

    next_scored, decision = session.next_question()
    return {
        "resolved_utterances": len(outcome.resolved),
        "resolved_sec": round(outcome.resolved_seconds, 2),
        "coverage": round(session._coverage(), 4),
        "learned": outcome.as_payload()["learned"],
        "next_question": (
            {"prompt_text": next_scored.question.prompt_text, "kind": next_scored.question.kind}
            if next_scored
            else None
        ),
        "stop": decision.as_payload(),
    }


@router.post("/review/skip")
async def skip_question(
    session_id: str,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Never ask this one again (the subject is marked covered)."""
    session, stats = await _review_or_404(db, _session_uuid(session_id), settings)
    prompt = str(body.get("question") or "")
    scored = next((s for s in session.ranked() if s.question.prompt_text == prompt), None)
    if scored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown question")
    session.covered_voices.update(scored.question.target_voices)
    session.answered_refs.update(scored.question.target_utterances)
    stats.belief = _snapshot_with(session, stats.belief)
    await db.commit()
    return {"skipped": prompt}


@router.post("/review/finish")
async def finish_review(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Stop reviewing. The session moves on (the review is never blocking).

    This is the event that starts the content pipeline: the review is over, the
    attributed artifact is final, and generation can read it. Nothing downstream
    waits for the DM to answer anything - 'Finish anyway' produces the same
    result as a completed review, which is what makes the stage skippable
    (docs/attribution-model.md S15.1).
    """
    session_id_uuid = _session_uuid(session_id)
    session, stats = await _review_or_404(db, session_id_uuid, settings)
    outcome = session.finish()
    stats.coverage_after = outcome.coverage_after
    stats.stop_reason = outcome.stop_reason
    run = await db.scalar(select(ReviewRun).where(ReviewRun.session_id == session_id_uuid))
    if run is not None:
        run.status = "complete"
        run.finished_at = datetime.now(UTC)
        run.coverage_after = outcome.coverage_after
    await db.commit()

    publisher = getattr(request.app.state, "publisher", None)
    if publisher is not None:
        from dnd_common.events import Event

        await publisher.publish(
            Event(
                type="attribution.review.completed",
                payload={
                    "session_id": session_id,
                    "campaign_id": str(stats.campaign_id),
                    "outcome": outcome.outcome,
                    "questions_asked": len(outcome.asked),
                    "coverage": round(outcome.coverage_after, 4),
                    "unresolved": stats.unresolved,
                    "review_run_id": str(run.id) if run is not None else None,
                },
            )
        )
    return outcome.as_payload()


@router.get("/scenes")
async def scene_view(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Where the session happens, and who the record puts there (S12.6).

    Read by the DM, who is the only one able to correct a reading of their own
    table: the panel shows each stretch with its place, its moment count and the
    characters the reading put in it and elsewhere. Nothing here is a decision -
    the exclusions are already baked into the belief - it is the evidence behind
    them, which is what makes them arguable.
    """
    session_id_uuid = _session_uuid(session_id)
    rows = (
        await db.scalars(
            select(SessionScene)
            .where(SessionScene.session_id == session_id_uuid)
            .order_by(SessionScene.index)
        )
    ).all()
    return {"session_id": session_id, "scenes": [scene_payload(row) for row in rows]}


def scene_payload(row: Any) -> dict[str, Any]:
    """One stored stretch, in the shape the panel reads.

    Pure and separate from the route for the same reason as review_payload: a
    route body is the one place in this service that no test reaches without a
    database, and a payload built inline there is a payload nothing checks.
    """
    return {
        "index": row.index,
        "location": row.location,
        "reason": row.reason,
        "moments": row.end_ordinal - row.start_ordinal + 1,
        "start_sec": float(row.start_sec) if row.start_sec is not None else None,
        "end_sec": float(row.end_sec) if row.end_sec is not None else None,
        "first_ref": row.first_ref,
        "last_ref": row.last_ref,
        "present": list(row.present_names or []),
        "absent": list(row.absent_names or []),
        "npcs": list(row.npcs or []),
        "present_member_ids": [str(value) for value in (row.present_member_ids or [])],
        "absent_member_ids": [str(value) for value in (row.absent_member_ids or [])],
        "revision": row.revision,
    }


@router.get("/attribution")
async def attribution_view(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Paginated per-utterance attribution (the provenance chips read this)."""
    session_id_uuid = _session_uuid(session_id)
    rows = (
        await db.execute(
            select(UtteranceAttribution, Utterance)
            .join(Utterance, Utterance.id == UtteranceAttribution.utterance_id)
            .where(UtteranceAttribution.session_id == session_id_uuid)
            .order_by(Utterance.ordinal)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    stats = await db.get(SessionBeliefStats, session_id_uuid)
    return {
        "session_id": session_id,
        "coverage": float(stats.coverage) if stats else 0.0,
        "engine_version": stats.engine_version if stats else None,
        "utterances": [
            {
                "ref": utterance.ref,
                "start": float(utterance.start_sec),
                "end": float(utterance.end_sec),
                "text": utterance.text,
                "status": attribution.status,
                "confidence": (
                    float(attribution.confidence)
                    if attribution.confidence is not None
                    else None
                ),
                "best_candidate": attribution.best_candidate,
                "decided_by": attribution.decided_by,
                "revision": attribution.revision,
            }
            for attribution, utterance in rows
        ],
    }


@router.get("/voices")
async def voices_view(
    session_id: str,
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """The "Voices we found" panel: anonymous identities, never people.

    Each voice carries what the engine currently makes of it - a GUESS with a
    number, never a verdict - because this panel is where a DM corrects a single
    voice once the old per-label panel is gone. Showing only a handle and a
    duration left them with nothing to disagree with.
    """
    session_id_uuid = _session_uuid(session_id)
    rows = (
        await db.scalars(
            select(VoiceIdentity)
            .where(VoiceIdentity.session_id == session_id_uuid)
            .order_by(VoiceIdentity.handle)
        )
    ).all()
    session, stats = await review_service.load_review(
        db,
        session_id_uuid,
        policy=_policy(settings),
        thresholds=_thresholds(settings),
    )
    guesses = _voice_guesses(session, stats) if session is not None and stats else {}
    return {
        "session_id": session_id,
        "voices": [
            {
                "id": str(row.id),
                "handle": row.handle,
                "speech_sec": float(row.speech_sec or 0),
                "purity": float(row.purity) if row.purity is not None else None,
                "observations": row.observation_count,
                "status": row.status,
                **guesses.get(row.handle, {}),
            }
            for row in rows
        ],
    }


def _voice_guesses(session, stats) -> dict[str, dict[str, Any]]:
    """handle -> what the engine will ACT on for that voice, and how much of it.

    Built from the VERDICTS, not from the raw posterior. That distinction is the
    whole point: inference is smooth and, once the mean-field fallback converges,
    over-confident - a voice whose every utterance carries nothing but the DM
    prior comes out at p=0.99 for the DM, while the statuses correctly refuse to
    promote any of it, because a lone prior is not a corroborating channel. The
    panel reports the share of the voice's speech that is CONFIDENTLY attributed
    to somebody, so it cannot claim more than the engine will act on: a DM shown
    "we think Bob, 99 %" for a guess that never reaches the wiki has been told
    something false.
    """
    from app.inference import best_candidate
    from app.statuses import is_confident

    belief = session.belief()
    result = session.propagator.infer(belief)
    verdicts = session.propagator.verdicts(belief, result)
    labels = {
        f"member:{entry.get('member_id')}": entry.get("label")
        or entry.get("character_name")
        or entry.get("member_id")
        for entry in (stats.belief or {}).get("roster") or []
    }
    answers = dict(belief.voice_answers)

    out: dict[str, dict[str, Any]] = {}
    for voice_id, state in belief.voices.items():
        spoken: dict[str, float] = {}
        sure: dict[str, float] = {}
        seconds = 0.0
        for ref in state.refs:
            node = belief.nodes.get(ref)
            posterior = result.posteriors.get(ref)
            if node is None or not posterior:
                continue
            seconds += node.seconds
            candidate, _ = best_candidate(posterior)
            if candidate is None:
                continue
            spoken[candidate] = spoken.get(candidate, 0.0) + node.seconds
            verdict = verdicts.get(ref)
            if verdict is not None and is_confident(verdict.status):
                sure[candidate] = sure.get(candidate, 0.0) + node.seconds
        if not spoken or seconds <= 0:
            continue
        # Alphabetically-first on a tie, like best_candidate(): the same
        # evidence must always name the same person, or the panel reshuffles
        # between page loads and reads as broken.
        leading = min(spoken, key=lambda key: (-spoken[key], key))
        out[str(state.id)] = {
            "guess": leading,
            "guess_label": labels.get(leading, "Someone not in the campaign"),
            "guess_confidence": round(sure.get(leading, 0.0) / seconds, 4),
            "confirmed": answers.get(voice_id),
        }
    return out


@router.post("/utterances/{ref}/attribute")
async def attribute_utterance(
    session_id: str,
    ref: str,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Power-user direct fix: write an explicit user_answer for one utterance."""
    session_id_uuid = _session_uuid(session_id)
    session, stats = await _review_or_404(db, session_id_uuid, settings)
    candidate = str(body.get("candidate") or "")
    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="candidate is required"
        )
    belief = session.belief()
    if ref not in belief.nodes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown utterance")
    belief.answers[ref] = candidate
    stats.belief = _snapshot_with(session, stats.belief)
    await db.commit()
    return {"ref": ref, "candidate": candidate, "coverage": round(session._coverage(), 4)}


@router.post("/voices/{voice_id}/split")
async def split_voice(
    session_id: str,
    voice_id: str,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Manual structural fix: this voice is more than one person."""
    session, stats = await _review_or_404(db, _session_uuid(session_id), settings)
    session.belief().split_voices.add(voice_id)
    stats.belief = _snapshot_with(session, stats.belief)
    await db.commit()
    return {"voice_id": voice_id, "split": True}


@router.post("/voices/merge")
async def merge_voices(
    session_id: str,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_session),
    settings: ServiceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Manual structural fix: these voices are one person."""
    session, stats = await _review_or_404(db, _session_uuid(session_id), settings)
    voice_ids = [str(v) for v in (body.get("voice_ids") or [])]
    if len(voice_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at least two voice ids are required",
        )
    target, *others = voice_ids
    for other in others:
        session.belief().merged_voices[other] = target
    stats.belief = _snapshot_with(session, stats.belief)
    await db.commit()
    return {"merged": others, "into": target}


def _snapshot_with(session, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Re-encode the live belief plus what the review already covered.

    'previous' is the stored snapshot, and it is CARRIED OVER rather than
    rebuilt: the belief is the part that changes when the DM answers, while the
    roster, the handles and the language describe the session and do not.
    Re-deriving them here would be a second source of truth, and dropping them -
    which is what this did - left every later read without the labels it needs
    to put a name on a voice.

    The belief itself goes through the SAME encoder the compute pass uses. This
    function used to carry its own copy of that dictionary, and the copy had
    already lost a key: 'propagated_refs', the record of which moments an answer
    moved. Losing it meant the next answer re-read the session as "everything is
    inferred", i.e. on the stricter status bar - so answering a question made
    the coverage the DM sees go DOWN, and it stayed down until the next
    recompute (see encode_belief_state).
    """
    snapshot: dict[str, Any] = dict(previous or {})
    snapshot.update(review_service.encode_belief_state(session.belief()))
    snapshot["asked_texts"] = [s.question.prompt_text for s in session.asked]
    return snapshot

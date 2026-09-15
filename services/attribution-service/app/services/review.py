"""Persistence for the review: plans in, questions and answers out.

The compute pass stores three things:

- the utterances and their current beliefs (queryable, joinable, auditable);
- the candidate questions and the run;
- a SNAPSHOT of the belief, so the API can rebuild a Propagator without
  re-running the identity-evidence pass on every page load.

The snapshot is a cache. utterance_evidence is the log the belief is derived
from, and a snapshot whose engine_version does not match the running engine is
discarded and the session recomputed.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.inference import Edge
from app.models import (
    ReviewQuestion,
    ReviewRun,
    SessionBeliefStats,
    Utterance,
    UtteranceAttribution,
    VoiceIdentity,
)
from app.pipeline import SessionPlan
from app.propagate import Belief, Node, Propagator, VoiceState
from app.review import ReviewPolicy, ReviewSession
from app.statuses import StatusThresholds

logger = logging.getLogger(__name__)


def _uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def encode_belief(plan: SessionPlan) -> dict[str, Any]:
    """Serialise everything needed to rebuild the Propagator."""
    belief = plan.belief
    return {
        "candidates": list(belief.candidates),
        "nodes": {
            ref: {
                "ref": node.ref,
                "voice_id": node.voice_id,
                "stakes": node.stakes,
                "seconds": node.seconds,
                "capability_reqs": list(node.capability_reqs),
                "text": node.text,
                "alpha": node.alpha,
                "similarity": node.similarity,
            }
            for ref, node in belief.nodes.items()
        },
        "potentials": {ref: dict(values) for ref, values in belief.potentials.items()},
        "edges": [
            {
                "left": edge.left,
                "right": edge.right,
                "log_same": edge.log_same,
                "log_diff": edge.log_diff,
                "kind": edge.kind,
            }
            for edge in belief.edges
        ],
        "voices": {
            voice_id: {
                "id": state.id,
                "refs": list(state.refs),
                "posterior": state.posterior,
                "purity": state.purity,
            }
            for voice_id, state in belief.voices.items()
        },
        "corroborating": dict(belief.corroborating),
        "voice_lr": dict(belief.voice_lr),
        "answers": dict(belief.answers),
        "voice_answers": dict(belief.voice_answers),
        "learned": {k: sorted(v) for k, v in belief.learned.items()},
        "split_voices": sorted(belief.split_voices),
        "merged_voices": dict(belief.merged_voices),
        # Omitted entirely when the set was never recorded (an old snapshot):
        # "unknown" and "nothing moved" are different claims, and the decode
        # below keeps them apart.
        **(
            {"propagated_refs": sorted(belief.propagated_refs)}
            if belief.propagated_refs is not None
            else {}
        ),
        "handles": dict(plan.handles),
        "roster": plan.roster,
        "language": plan.language,
    }


def decode_belief(snapshot: Mapping[str, Any]) -> Belief:
    """Rebuild a Belief from a snapshot (see encode_belief)."""
    candidates = list(snapshot.get("candidates") or [])
    nodes = {
        ref: Node(
            ref=ref,
            voice_id=payload.get("voice_id"),
            stakes=float(payload.get("stakes", 0.5)),
            seconds=float(payload.get("seconds", 0.0)),
            capability_reqs=tuple(payload.get("capability_reqs") or ()),
            text=str(payload.get("text") or ""),
            alpha=float(payload.get("alpha", 1.0)),
            similarity={k: float(v) for k, v in (payload.get("similarity") or {}).items()},
        )
        for ref, payload in (snapshot.get("nodes") or {}).items()
    }
    edges = [
        Edge(
            left=str(entry["left"]),
            right=str(entry["right"]),
            log_same=float(entry["log_same"]),
            log_diff=float(entry["log_diff"]),
            kind=str(entry.get("kind") or "same_voice"),
        )
        for entry in snapshot.get("edges") or []
    ]
    voices = {
        voice_id: VoiceState(
            id=voice_id,
            refs=tuple(payload.get("refs") or ()),
            posterior={k: float(v) for k, v in (payload.get("posterior") or {}).items()},
            # An unmeasured purity round-trips as None, never as 1.0: the
            # snapshot is what the review and the question ranking re-infer
            # from, so a decode that invented a measurement would undo the fix
            # on every page load.
            purity=(
                float(payload["purity"])
                if payload.get("purity") is not None
                else None
            ),
        )
        for voice_id, payload in (snapshot.get("voices") or {}).items()
    }
    return Belief(
        candidates=candidates,
        nodes=nodes,
        potentials={
            ref: {k: float(v) for k, v in values.items()}
            for ref, values in (snapshot.get("potentials") or {}).items()
        },
        edges=edges,
        voices=voices,
        corroborating=dict(snapshot.get("corroborating") or {}),
        voice_lr={k: float(v) for k, v in (snapshot.get("voice_lr") or {}).items()},
        answers=dict(snapshot.get("answers") or {}),
        voice_answers=dict(snapshot.get("voice_answers") or {}),
        learned={k: set(v) for k, v in (snapshot.get("learned") or {}).items()},
        split_voices=set(snapshot.get("split_voices") or []),
        merged_voices=dict(snapshot.get("merged_voices") or {}),
        propagated_refs=(
            set(snapshot["propagated_refs"])
            if snapshot.get("propagated_refs") is not None
            else None
        ),
    )


async def store_plan(
    db: AsyncSession,
    plan: SessionPlan,
    *,
    revision: int,
    thresholds: StatusThresholds,
    policy: ReviewPolicy,
    questions_planned: int | None = None,
) -> ReviewRun:
    """Persist a computed plan: utterances, belief, questions and the run."""
    session_id = _uuid(plan.session_id)
    campaign_id = _uuid(plan.campaign_id)
    if session_id is None or campaign_id is None:
        raise ValueError("session_id and campaign_id must be uuids")

    for utterance in plan.utterances:
        row = await db.get(Utterance, _utterance_id(session_id, utterance.ref))
        if row is None:
            row = Utterance(
                id=_utterance_id(session_id, utterance.ref),
                session_id=session_id,
                campaign_id=campaign_id,
                ordinal=utterance.ordinal,
                ref=utterance.ref,
            )
            db.add(row)
        row.start_sec = utterance.start
        row.end_sec = utterance.end
        row.text = utterance.text
        row.diar_label = utterance.diar_label
        row.diar_chunk = utterance.diar_chunk
        row.segment_indices = list(utterance.segment_indices)
        row.voice_id = None
        item = plan.evidence.item(utterance.ref)
        row.kind = item.kind
        row.voice_mode = item.voice_mode
        row.gist = item.gist
        row.stakes = item.stakes
        row.capability_reqs = list(item.capability_requirements)
        row.claimed_names = [c.get("value", "") for c in item.claims]
        row.addressed_names = [a.get("name", "") for a in item.addresses]

        posterior = plan.inference.posteriors.get(utterance.ref, {})
        verdict = plan.verdicts.get(utterance.ref)
        from app.inference import best_candidate
        from app.inference import margin as posterior_margin

        best, confidence = best_candidate(posterior)
        attribution = await db.get(UtteranceAttribution, row.id)
        if attribution is None:
            attribution = UtteranceAttribution(
                utterance_id=row.id, session_id=session_id, posterior={}
            )
            db.add(attribution)
        attribution.posterior = {k: round(v, 6) for k, v in posterior.items()}
        attribution.best_candidate = best
        attribution.best_member_id = _uuid(
            best.split(":", 1)[1]
            if best and best.startswith("member:")
            else None
        )
        attribution.confidence = confidence
        attribution.margin = posterior_margin(posterior)
        attribution.entropy = verdict.entropy if verdict else None
        attribution.status = verdict.status if verdict else "unresolved"
        attribution.decided_by = verdict.decided_by if verdict else None
        attribution.revision = revision

    # voice identities (the "Voices we found" panel reads these)
    await db.execute(delete(VoiceIdentity).where(VoiceIdentity.session_id == session_id))
    for voice_id, state in plan.voices.items():
        db.add(
            VoiceIdentity(
                id=_voice_uuid(session_id, voice_id),
                session_id=session_id,
                campaign_id=campaign_id,
                handle=plan.handles.get(voice_id, voice_id),
                start_sec=min(
                    (u.start for u in plan.utterances if u.ref in state.refs), default=None
                ),
                end_sec=max(
                    (u.end for u in plan.utterances if u.ref in state.refs), default=None
                ),
                observation_count=len(state.refs),
                speech_sec=sum(
                    u.duration for u in plan.utterances if u.ref in state.refs
                ),
                purity=state.purity,
                impurity_evidence=[],
                scope="session",
                status="open",
            )
        )

    await db.execute(delete(ReviewQuestion).where(ReviewQuestion.session_id == session_id))
    for question in plan.questions:
        db.add(
            ReviewQuestion(
                session_id=session_id,
                campaign_id=campaign_id,
                revision=revision,
                kind=question.kind,
                prompt_text=question.prompt_text,
                hook=question.hook,
                options=[option.as_payload() for option in question.options],
                target_utterances=[
                    _utterance_id(session_id, ref) for ref in question.target_utterances
                ],
                target_voices=[
                    _voice_uuid(session_id, vid) for vid in question.target_voices
                ],
                cost=question.cost,
                status="candidate",
            )
        )

    run = await db.scalar(select(ReviewRun).where(ReviewRun.session_id == session_id))
    if run is None:
        run = ReviewRun(session_id=session_id, campaign_id=campaign_id)
        db.add(run)
    run.campaign_id = campaign_id
    run.status = "pending"
    run.questions_planned = questions_planned
    run.coverage_before = plan.coverage
    run.engine_version = plan.engine_version
    run.converged = plan.converged
    run.started_at = run.started_at or datetime.now(UTC)

    stats = await db.get(SessionBeliefStats, session_id)
    if stats is None:
        stats = SessionBeliefStats(session_id=session_id, campaign_id=campaign_id)
        db.add(stats)
    stats.campaign_id = campaign_id
    stats.revision = revision
    stats.coverage = plan.coverage
    stats.unresolved = sum(
        1 for v in plan.verdicts.values() if v.status == "unresolved"
    )
    stats.unresolved_stakes = plan.unresolved
    stats.total_speech_sec = sum(u.duration for u in plan.utterances)
    stats.entropy = sum(v.entropy for v in plan.verdicts.values())
    stats.questions_planned = questions_planned
    stats.converged = plan.converged
    stats.belief = json.loads(json.dumps(encode_belief(plan), default=str))
    stats.engine_version = plan.engine_version
    stats.computed_at = datetime.now(UTC)

    await db.commit()
    return run


def _utterance_id(session_id: uuid.UUID, ref: str) -> uuid.UUID:
    """Deterministic id, so a recompute updates rows instead of duplicating them."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dnd:utterance:{session_id}:{ref}")


def _voice_uuid(session_id: uuid.UUID, voice_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dnd:voice:{session_id}:{voice_id}")


async def load_review(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    policy: ReviewPolicy | None = None,
    thresholds: StatusThresholds | None = None,
) -> tuple[ReviewSession | None, SessionBeliefStats | None]:
    """Rebuild the review from the stored snapshot (None when never computed)."""
    stats = await db.get(SessionBeliefStats, session_id)
    if stats is None or not stats.belief:
        return None, None
    snapshot = stats.belief
    belief = decode_belief(snapshot)
    if thresholds is not None:
        belief.thresholds = thresholds
    propagator = Propagator(belief)
    questions = await load_questions(
        db,
        session_id,
        stakes={ref: node.stakes for ref, node in belief.nodes.items()},
    )
    session = ReviewSession(
        propagator=propagator, questions=questions, policy=policy or ReviewPolicy()
    )
    # "The DM finished this review" is a property of the RUN, and it lives in the
    # database rather than in the process: keeping it only in memory meant the
    # card came back, "in review", on the next page load - and `next_question`
    # happily started asking again - for a review the DM had closed.
    session.finished = (await _run_status(db, session_id)) == "complete"
    asked = [q for q in questions if q.prompt_text in _asked_texts(snapshot)]
    from app.ranking import ScoredQuestion

    for question in asked:
        session.asked.append(
            ScoredQuestion(
                question=question,
                expected_gain=0.0,
                answer_probabilities={},
                entropy_after={},
                uninformative_penalty=0.0,
                overlap_penalty=0.0,
                score=0.0,
            )
        )
    return session, stats


def _asked_texts(snapshot: Mapping[str, Any]) -> set[str]:
    return set(snapshot.get("asked_texts") or [])


async def _run_status(db: AsyncSession, session_id: uuid.UUID) -> str:
    """The stored review run's status ("pending" when there is no run yet)."""
    status = await db.scalar(
        select(ReviewRun.status).where(ReviewRun.session_id == session_id)
    )
    return str(status or "pending")


async def load_questions(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    stakes: Mapping[str, float] | None = None,
) -> list[Any]:
    """The stored candidate questions, rebuilt as generator objects.

    The rows carry utterance and voice IDs - the tables' own keys - while the
    engine works in utterance REFS ("u_00001") and voice HANDLES ("V1"). The two
    are NOT interchangeable, and putting the ids straight back into the question
    made every stored question inert: the answer was written onto a key no node
    had, so the simulated gain of answering it was zero, so the review planned
    nothing and the DM was asked nothing at all. Resolving here is what makes a
    question loaded from the database the same question that was stored.
    """

    rows = (
        await db.scalars(
            select(ReviewQuestion).where(
                ReviewQuestion.session_id == session_id,
                ReviewQuestion.status.in_(("candidate", "asked")),
            )
        )
    ).all()

    ref_of = {
        row_id: ref
        for row_id, ref in (
            await db.execute(
                select(Utterance.id, Utterance.ref).where(
                    Utterance.session_id == session_id
                )
            )
        ).all()
    }
    voice_of = {
        row_id: handle
        for row_id, handle in (
            await db.execute(
                select(VoiceIdentity.id, VoiceIdentity.handle).where(
                    VoiceIdentity.session_id == session_id
                )
            )
        ).all()
    }

    return rebuild_questions(rows, ref_of=ref_of, voice_of=voice_of, stakes=stakes)


def rebuild_questions(
    rows: Sequence[Any],
    *,
    ref_of: Mapping[uuid.UUID, str],
    voice_of: Mapping[uuid.UUID, str],
    stakes: Mapping[str, float] | None = None,
) -> list[Any]:
    """Turn stored question rows into engine questions (see load_questions)."""
    from app.questions import CandidateQuestion, QuestionOption

    stakes = stakes or {}
    out: list[CandidateQuestion] = []
    for row in rows:
        stored_refs = list(row.target_utterances or [])
        stored_voices = list(row.target_voices or [])
        targets = [ref_of[item] for item in stored_refs if item in ref_of]
        voices = [voice_of[item] for item in stored_voices if item in voice_of]
        if not targets and not voices:
            # Nothing resolvable at all: the question cannot be asked, and
            # keeping it would put an inert candidate in front of the ranking.
            logger.warning(
                "dropping stored question %s: none of its %d utterance(s) or "
                "%d voice(s) resolve",
                row.id,
                len(stored_refs),
                len(stored_voices),
            )
            continue
        out.append(
            CandidateQuestion(
                kind=row.kind,
                prompt_text=row.prompt_text,
                options=[
                    QuestionOption(
                        key=str(option.get("key")),
                        label=str(option.get("label")),
                        why=option.get("why"),
                    )
                    for option in (row.options or [])
                ],
                target_utterances=targets,
                target_voices=voices,
                hook=row.hook or {},
                cost=float(row.cost or 1.0),
                # The stakes of the moments it is about, not a constant: the
                # ranking prices the DM's effort against how much the wiki will
                # use the answer.
                mean_stakes=(
                    sum(stakes.get(ref, 0.5) for ref in targets) / len(targets)
                    if targets
                    else 0.7
                ),
                note=f"stored:{row.id}",
            )
        )
    return out

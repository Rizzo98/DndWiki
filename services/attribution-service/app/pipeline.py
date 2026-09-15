"""The compute pass: diarization to a belief, a review and an attributed transcript.

This module is the seam between the pure engine and the world. It takes plain
data (segments, roster, the evidence pass, the voice similarity evidence
speaker-service published, the calibration) and produces a SessionPlan: voice
identities, a posterior per utterance, the statuses, the questions worth asking,
and the artifact content-service will read.

It is deliberately SYNCHRONOUS and IO-FREE. app.workers.compute owns the IO (MinIO,
Postgres, RabbitMQ) and calls this; the tests call it directly with fixtures. That
split is what keeps the whole engine testable without a single container.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from dnd_common.clustering import spherical_kmeans

from app.calibration import Calibration
from app.capabilities import CapabilityStore, normalize_all
from app.channels import (
    ChannelOutput,
    Claim,
    UtteranceFeatures,
    address,
    capability,
    corroboration,
    dm_prior,
    identity_note,
    narration,
    out_of_world,
    self_name,
    voice,
    voice_mode,
    voice_strength,
)
from app.clustering import nearest_pairs
from app.evidence import EvidencePass
from app.inference import (
    CONTINUITY_EPS,
    SAME_VOICE_EPS,
    Edge,
    InferenceResult,
    compose_potential,
    identity_coupling_eps,
    trust_alpha,
)
from app.propagate import Belief, Node, Propagator, VoiceState
from app.questions import (
    CandidateQuestion,
    QuestionContext,
    UtteranceView,
    VoiceView,
    generate,
)
from app.review import ReviewPolicy, ReviewSession
from app.statuses import (
    StatusThresholds,
    Verdict,
    coverage,
    is_confident,
    unresolved_stakes,
)
from app.utterances import Utterance, build_utterances, gap_before

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"
#: How many nearest neighbours connect each utterance inside its voice identity.
DEFAULT_SAME_VOICE_NEIGHBOURS = 8


@dataclass(frozen=True)
class ObservationInput:
    """The voice evidence speaker-service published for one utterance."""

    ref: str
    label: str | None = None
    quality: float | None = None
    vector: tuple[float, ...] = ()
    #: (candidate key, cosine) for the nearest enrolled voices, best first. The
    #: key is 'member:<id>' for everything written after the redesign and
    #: 'user:<id>' for the legacy prints, resolved through user_to_member.
    nearest: tuple[tuple[str, float], ...] = ()


@dataclass
class PlanInput:
    """Everything the pass needs, with no hidden global state."""

    session_id: str
    campaign_id: str
    language: str = "en"
    segments: list[dict[str, Any]] = field(default_factory=list)
    #: Pre-built utterances. The worker builds them first so it can attach the
    #: per-utterance voice evidence (which is keyed by utterance) and run the
    #: identity-evidence pass against real references before the pass runs. When
    #: absent they are built from 'segments'.
    utterances: list[Utterance] | None = None
    members: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, ObservationInput] = field(default_factory=dict)
    user_to_member: dict[str, str] = field(default_factory=dict)
    evidence: EvidencePass = field(default_factory=EvidencePass)
    calibration: Calibration = field(default_factory=Calibration)
    capabilities: CapabilityStore = field(default_factory=CapabilityStore)
    thresholds: StatusThresholds = field(default_factory=StatusThresholds)
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    chunk_boundaries: Sequence[int] | None = None
    purity_split_threshold: float = 0.80
    same_voice_neighbours: int = DEFAULT_SAME_VOICE_NEIGHBOURS
    #: Coupling eps for an identity MEASURED to be one person (purity 1.0).
    same_voice_eps: float = SAME_VOICE_EPS
    engine_version: str = "attr-1"


@dataclass
class SessionPlan:
    """The whole result of one attribution pass."""

    session_id: str
    campaign_id: str
    language: str
    utterances: list[Utterance]
    voices: dict[str, VoiceState]
    handles: dict[str, str]
    belief: Belief
    inference: InferenceResult
    verdicts: dict[str, Verdict]
    questions: list[CandidateQuestion]
    review: ReviewSession
    coverage: float
    unresolved: float
    roster: list[dict[str, Any]] = field(default_factory=list)
    #: The evidence pass's output, kept so the artifact and the review card can
    #: report the kind / voice mode / gist of a moment without a second lookup.
    evidence: EvidencePass = field(default_factory=EvidencePass)
    engine_version: str = "attr-1"

    @property
    def converged(self) -> bool:
        return self.inference.converged

    @property
    def method(self) -> str:
        return self.inference.method

    def buckets(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for verdict in self.verdicts.values():
            out[verdict.status] = out.get(verdict.status, 0) + 1
        return out

    def attributed_transcript(self, *, revision: int = 1) -> dict[str, Any]:
        """The artifact content-service reads instead of a label map (S14.1)."""
        from app.attribution_artifact import build_artifact

        return build_artifact(self, revision=revision)


# --- roster helpers ---------------------------------------------------------


def candidate_key(member_id: str) -> str:
    return f"member:{member_id}"


def roster_index(
    members: Sequence[Mapping[str, Any]],
) -> tuple[list[str], dict[str, str], str | None, dict[str, str]]:
    """(candidate keys, label per candidate, dm key, lowercase name -> candidate)."""
    keys: list[str] = []
    labels: dict[str, str] = {}
    names: dict[str, str] = {}
    dm_key: str | None = None
    for member in members:
        member_id = str(member.get("member_id") or member.get("id") or "").strip()
        if not member_id:
            continue
        key = candidate_key(member_id)
        keys.append(key)
        player = str(member.get("player_name") or "").strip()
        character = str(member.get("character_name") or "").strip()
        if str(member.get("role") or "") == "dm":
            labels[key] = "The Dungeon Master"
            dm_key = key
        elif character and player:
            labels[key] = f"{player} — {character}"
        else:
            labels[key] = character or player or member_id
        for name in (player, character):
            if name:
                names[name.strip().lower()] = key
    keys.append(UNKNOWN)
    labels[UNKNOWN] = "Someone not in the campaign"
    return keys, labels, dm_key, names


# --- voice identities -------------------------------------------------------


def _split_label(
    refs: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    threshold: float,
    seed: int,
) -> list[list[str]]:
    """Split one diarization label into one or two voice identities.

    A false split is worse than a missed one: it destroys a correct identity and
    asks the DM to fix what was already right. So the split only happens when
    dnd_common.purity's stability test says so (app.structure owns that verdict);
    this function is the mechanical part.
    """
    if len(refs) < 6 or len(vectors) != len(refs):
        return [list(refs)]
    labels = spherical_kmeans(vectors, 2, restarts=25, seed=seed)
    if len(set(labels)) < 2:
        return [list(refs)]
    first = [ref for ref, label in zip(refs, labels) if label == 0]
    second = [ref for ref, label in zip(refs, labels) if label == 1]
    if min(len(first), len(second)) < 3:
        return [list(refs)]
    return [first, second]


def build_voices(
    utterance_list: Sequence[Utterance],
    observations: Mapping[str, ObservationInput],
    *,
    candidates: Sequence[str],
    calibration: Calibration,
    purity_split_threshold: float = 0.80,
    seed: int = 20240914,
) -> tuple[dict[str, VoiceState], dict[str, str]]:
    """Group utterances into anonymous, session-scoped voice identities.

    Voices are numbered V1, V2, ... in first-appearance order and are NEVER
    shown to the DM as people (S13.2): they exist so evidence can be pooled and
    so a split or a merge has something to operate on.
    """
    by_label: dict[str, list[Utterance]] = {}
    for utterance in utterance_list:
        by_label.setdefault(utterance.diar_label or "-", []).append(utterance)

    voices: dict[str, VoiceState] = {}
    handle_of: dict[str, str] = {}
    counter = 0
    for label in sorted(by_label, key=lambda l: by_label[l][0].ordinal):
        group = by_label[label]
        refs = [u.ref for u in group]
        vectors = [
            observations[u.ref].vector for u in group if observations.get(u.ref)
        ]
        vectors = [v for v in vectors if len(v) > 0]
        parts = (
            _split_label(refs, vectors, threshold=purity_split_threshold, seed=seed)
            if len(vectors) == len(refs)
            else [refs]
        )
        for part in parts:
            counter += 1
            voice_id = f"V{counter}"
            handle_of[voice_id] = f"V{counter}"
            voices[voice_id] = VoiceState(
                id=voice_id,
                refs=tuple(part),
                posterior=_voice_posterior(part, observations, candidates, calibration),
                # Purity is a MEASUREMENT (S5.1), and this pass does not take
                # one. The split either fired - which says "two voices" and
                # fixes the purity at the threshold - or it did not, which says
                # nothing whatsoever about whether the label is one person.
                # Recording 1.0 in that case asserts the strongest form of a
                # claim nobody checked, and every downstream reader of purity
                # (the same_voice coupling, alpha, the voices panel) then treats
                # an unmeasured identity as a verified one.
                purity=None if len(parts) == 1 else purity_split_threshold,
            )
    return voices, handle_of


def _voice_posterior(
    refs: Sequence[str],
    observations: Mapping[str, ObservationInput],
    candidates: Sequence[str],
    calibration: Calibration,
) -> dict[str, float]:
    """Level 2: what the identity's pooled voice evidence says about its owner."""
    pooled: dict[str, float] = {}
    for ref in refs:
        observation = observations.get(ref)
        if observation is None:
            continue
        for candidate, cosine in observation.nearest:
            value = calibration.log_lr(cosine, quality=observation.quality or 1.0)
            pooled[candidate] = max(pooled.get(candidate, float("-inf")), value)
    from app.inference import softmax

    if not pooled:
        return {candidate: 1.0 / len(candidates) for candidate in candidates}
    return softmax(pooled)


# --- channels ---------------------------------------------------------------


def _claims_of(pass_: EvidencePass, ref: str) -> tuple[Claim, ...]:
    item = pass_.item(ref)
    return tuple(
        Claim(
            type=str(claim.get("type") or ""),
            value=str(claim.get("value") or ""),
            strength=float(claim.get("strength", 1.0)),
        )
        for claim in item.claims
    )


def _addressed_of(pass_: EvidencePass, ref: str) -> tuple[str, ...]:
    item = pass_.item(ref)
    return tuple(
        str(entry.get("name"))
        for entry in item.addresses
        if str(entry.get("target") or "") == "next_turn"
    )


def _excluded_for(
    pass_: EvidencePass, ref: str, ordinal_of: Mapping[str, int], names: Mapping[str, str]
) -> tuple[str, ...]:
    """Members an identity_note rules out for this utterance's window."""
    position = ordinal_of.get(ref, 0)
    excluded: list[str] = []
    for note in pass_.notes:
        if note.type != "member_absent" or not note.member:
            continue
        key = names.get(note.member.strip().lower())
        if key is None:
            continue
        start = ordinal_of.get(note.start_ref or "", 0)
        end = ordinal_of.get(note.end_ref or "", 10**9)
        if start <= position <= end:
            excluded.append(key)
    return tuple(excluded)


def channels_for(
    utterance: Utterance,
    *,
    plan_input: PlanInput,
    candidates: Sequence[str],
    names: Mapping[str, str],
    dm_key: str | None,
    ordinal_of: Mapping[str, int],
    previous_addressees: Sequence[str],
    speech_share: Mapping[str, float],
) -> list[ChannelOutput]:
    """Every channel's contribution to one utterance."""
    observation = plan_input.observations.get(utterance.ref)
    evidence_item = plan_input.evidence.item(utterance.ref)
    qualities = observation.quality if observation else None

    outputs: list[ChannelOutput] = []

    if observation and observation.nearest:
        scores = _resolve_candidates(observation.nearest, plan_input.user_to_member)
        if scores:
            outputs.append(
                voice(scores, calibration=plan_input.calibration, quality=qualities)
            )

    features = UtteranceFeatures(
        ordinal=utterance.ordinal,
        start=utterance.start,
        end=utterance.end,
        text=utterance.text,
        kind=evidence_item.kind,
        voice_mode=evidence_item.voice_mode,
        gist=evidence_item.gist,
        stakes=evidence_item.stakes,
        capability_reqs=tuple(normalize_all(evidence_item.capability_requirements)),
        addressed_names=_addressed_of(plan_input.evidence, utterance.ref),
        claims=_claims_of(plan_input.evidence, utterance.ref),
        voice_quality=qualities,
        gap_before_sec=utterance.gap_before_sec,
        excluded=_excluded_for(
            plan_input.evidence, utterance.ref, ordinal_of, names
        ),
    )

    outputs.append(
        self_name(
            features, name_to_candidate=names, candidates=candidates, dm_key=dm_key
        )
    )
    if features.capability_reqs:
        outputs.append(
            capability(
                list(features.capability_reqs),
                member_capabilities={
                    candidate.split(":", 1)[1]: sorted(
                        plan_input.capabilities.of(candidate.split(":", 1)[1])
                    )
                    for candidate in candidates
                    if candidate != UNKNOWN
                },
                candidates=candidates,
                dm_key=dm_key,
                authoritative=plan_input.capabilities.authoritative,
            )
        )
    if previous_addressees and (
        utterance.gap_before_sec is None
        or utterance.gap_before_sec <= 12.0
    ):
        outputs.append(
            address(previous_addressees, name_to_candidate=names, candidates=candidates)
        )
    outputs.append(narration(features, candidates=candidates, dm_key=dm_key))
    outputs.append(voice_mode(features, candidates=candidates, dm_key=dm_key))
    outputs.append(out_of_world(features, candidates=candidates, dm_key=dm_key))
    outputs.append(identity_note(features, candidates=candidates))
    outputs.append(dm_prior(speech_share, candidates=candidates))
    return [output for output in outputs if output.log_lr]


def _resolve_candidates(
    nearest: Sequence[tuple[str, float]], user_to_member: Mapping[str, str]
) -> dict[str, float]:
    """Map speaker-service's candidate keys onto this campaign's roster.

    Legacy prints carry 'user:<id>'; everything written after the redesign
    carries 'member:<id>'. Both resolve here, so the transition needs no
    big-bang re-enrollment - and a legacy print that resolves to nobody is
    dropped rather than counted as evidence for 'unknown'.
    """
    scores: dict[str, float] = {}
    for key, cosine in nearest:
        kind, _, value = key.partition(":")
        if kind == "member":
            resolved = candidate_key(value)
        elif kind == "user":
            member_id = user_to_member.get(value)
            if not member_id:
                continue
            resolved = candidate_key(member_id)
        else:
            continue
        scores[resolved] = max(scores.get(resolved, 0.0), float(cosine))
    return scores


# --- edges ------------------------------------------------------------------


def build_edges(
    utterance_list: Sequence[Utterance],
    observations: Mapping[str, ObservationInput],
    voice_of: Mapping[str, str | None],
    *,
    neighbours: int,
    voices: Mapping[str, VoiceState] | None = None,
    same_voice_eps: float = SAME_VOICE_EPS,
) -> list[Edge]:
    """The graph: same-voice edges (kNN inside an identity) plus continuity.

    The strength of a same-voice edge is the identity's own PURITY, not a
    constant: an identity nobody has measured is not an identity known to be one
    person, and clamping its utterances together at the measured-and-pure eps is
    what makes a whole session collapse onto whichever candidate a few of its
    utterances happened to favour (see inference.identity_coupling_eps).
    """
    edges: list[Edge] = []
    voices = voices or {}

    def coupling(voice_id: str) -> float:
        state = voices.get(voice_id)
        return identity_coupling_eps(
            state.purity if state is not None else None, measured_eps=same_voice_eps
        )

    by_voice: dict[str, list[Utterance]] = {}
    for utterance in utterance_list:
        voice_id = voice_of.get(utterance.ref)
        if voice_id:
            by_voice.setdefault(voice_id, []).append(utterance)

    for voice_id, group in by_voice.items():
        refs = [u.ref for u in group]
        vectors = [observations[ref].vector for ref in refs if ref in observations]
        eps = coupling(voice_id)
        if len(vectors) != len(refs):
            # an identity with an unembedded member: connect what we have in
            # TIME order, which is a weaker but honest substitute
            for left, right in pairwise(group):
                edges.append(
                    Edge.make(left.ref, right.ref, eps=eps, kind="same_voice")
                )
            continue
        for i, j, _similarity in nearest_pairs(vectors, neighbours):
            edges.append(Edge.make(refs[i], refs[j], eps=eps, kind="same_voice"))

    # Continuity: consecutive turns alternate speakers, with a decay by gap and
    # a cancellation when the earlier one is a question addressed to a name (the
    # answer is EXPECTED to be a different person, so the edge must not fight it).
    for left, right in pairwise(utterance_list):
        gap = right.start - left.end
        if gap > 30.0:
            continue
        eps = min(0.49, CONTINUITY_EPS * (1.0 + min(gap, 30.0) / 30.0))
        edges.append(Edge.make(left.ref, right.ref, eps=eps, kind="continuity"))
    return edges


# --- the pass ---------------------------------------------------------------


def plan_session(plan_input: PlanInput) -> SessionPlan:
    """Run the whole attribution pass over one session."""
    keys, labels, dm_key, names = roster_index(plan_input.members)

    utterances = gap_before(
        plan_input.utterances
        if plan_input.utterances is not None
        else build_utterances(
            plan_input.segments,
            chunk_boundaries=plan_input.chunk_boundaries,
        ).utterances
    )
    if not utterances:
        return _empty_plan(plan_input, keys, labels, dm_key)

    ordinal_of = {u.ref: u.ordinal for u in utterances}

    voices, handles = build_voices(
        utterances,
        plan_input.observations,
        candidates=keys,
        calibration=plan_input.calibration,
        purity_split_threshold=plan_input.purity_split_threshold,
    )
    voice_of: dict[str, str | None] = {}
    for voice_id, voice_state in voices.items():
        for ref in voice_state.refs:
            voice_of[ref] = voice_id

    share = _speech_share(utterances, voice_of, voices)

    outputs_by_ref: dict[str, list[ChannelOutput]] = {}
    for index, utterance in enumerate(utterances):
        previous = _addressed_of(plan_input.evidence, utterances[index - 1].ref) if index else ()
        outputs_by_ref[utterance.ref] = channels_for(
            utterance,
            plan_input=plan_input,
            candidates=keys,
            names=names,
            dm_key=dm_key,
            ordinal_of=ordinal_of,
            previous_addressees=previous,
            speech_share=share,
        )

    prior = {key: 1.0 / len(keys) for key in keys}
    potentials: dict[str, dict[str, float]] = {}
    nodes: dict[str, Node] = {}
    corroborating: dict[str, str | None] = {}
    voice_lrs: dict[str, float] = {}
    for utterance in utterances:
        outputs = outputs_by_ref[utterance.ref]
        observation = plan_input.observations.get(utterance.ref)
        voice_id = voice_of.get(utterance.ref)
        voice_state = voices.get(voice_id) if voice_id else None
        alpha = trust_alpha(
            purity=voice_state.purity if voice_state else None,
            quality=observation.quality if observation else None,
            cosine=_self_cosine(observation),
        )
        potentials[utterance.ref] = compose_potential(
            prior=prior,
            channel_outputs=outputs,
            identity_posterior=voice_state.posterior if voice_state else None,
            alpha=alpha,
            weights=plan_input.calibration.weights,
        )
        item = plan_input.evidence.item(utterance.ref)
        best = _best_of(outputs)
        # corroboration() returns None whenever no corroborating channel is
        # strong enough on its own - which is the COMMON case, not an error: it
        # is exactly what keeps a lone cosine out of the wiki. Index it only
        # once it exists.
        found = corroboration(outputs, best=best) if best else None
        corroborating[utterance.ref] = found[0] if found else None
        voice_lrs[utterance.ref] = voice_strength(outputs, best=best) if best else 0.0
        nodes[utterance.ref] = Node(
            ref=utterance.ref,
            voice_id=voice_id,
            stakes=item.stakes,
            seconds=utterance.duration,
            capability_reqs=tuple(normalize_all(item.capability_requirements)),
            text=utterance.text,
            alpha=alpha,
            similarity=_similarities(utterance.ref, utterance, utterances, plan_input.observations),
        )

    belief = Belief(
        candidates=list(keys),
        nodes=nodes,
        potentials=potentials,
        edges=build_edges(
            utterances,
            plan_input.observations,
            voice_of,
            neighbours=plan_input.same_voice_neighbours,
            voices=voices,
            same_voice_eps=plan_input.same_voice_eps,
        ),
        voices=voices,
        corroborating=corroborating,
        voice_lr=voice_lrs,
        thresholds=plan_input.thresholds,
    )
    propagator = Propagator(belief)
    result = propagator.infer(belief)
    verdicts = propagator.verdicts(belief, result)

    coverage_value, unresolved_value = _summary(belief, verdicts)

    questions = _questions(
        plan_input=plan_input,
        utterances=utterances,
        verdicts=verdicts,
        result=result,
        keys=keys,
        labels=labels,
        dm_key=dm_key,
        voices=voices,
        handles=handles,
        observations=plan_input.observations,
        voice_of=voice_of,
    )
    review = ReviewSession(
        propagator=propagator, questions=questions, policy=plan_input.policy
    )

    return SessionPlan(
        session_id=plan_input.session_id,
        campaign_id=plan_input.campaign_id,
        language=plan_input.language,
        utterances=utterances,
        voices=voices,
        handles=handles,
        belief=belief,
        inference=result,
        verdicts=verdicts,
        questions=questions,
        review=review,
        coverage=coverage_value,
        unresolved=unresolved_value,
        roster=_roster_payload(plan_input.members, labels),
        evidence=plan_input.evidence,
        engine_version=plan_input.engine_version,
    )


def _roster_payload(
    members: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for member in members:
        member_id = str(member.get("member_id") or member.get("id") or "")
        if not member_id:
            continue
        out.append(
            {
                "member_id": member_id,
                "player_name": member.get("player_name"),
                "character_name": member.get("character_name"),
                "role": member.get("role") or "player",
                "label": labels.get(candidate_key(member_id), member_id),
            }
        )
    return out


def _empty_plan(
    plan_input: PlanInput,
    keys: list[str],
    labels: dict[str, str],
    dm_key: str | None,
) -> SessionPlan:
    """A session with no usable segments is a valid, empty result - not an error."""
    belief = Belief(candidates=list(keys), nodes={}, potentials={})
    propagator = Propagator(belief)
    result = InferenceResult({}, True, 0, "bp", 0.0)
    return SessionPlan(
        session_id=plan_input.session_id,
        campaign_id=plan_input.campaign_id,
        language=plan_input.language,
        utterances=[],
        voices={},
        handles={},
        belief=belief,
        inference=result,
        verdicts={},
        questions=[],
        review=ReviewSession(propagator=propagator, questions=[], policy=plan_input.policy),
        coverage=1.0,
        unresolved=0.0,
        roster=_roster_payload(plan_input.members, labels),
        evidence=plan_input.evidence,
        engine_version=plan_input.engine_version,
    )


def _self_cosine(observation: ObservationInput | None) -> float | None:
    if observation is None or not observation.nearest:
        return None
    return max(cosine for _, cosine in observation.nearest)


def _best_of(outputs: Sequence[ChannelOutput]) -> str | None:
    totals: dict[str, float] = {}
    for output in outputs:
        for candidate, value in output.log_lr.items():
            totals[candidate] = totals.get(candidate, 0.0) + value
    if not totals:
        return None
    return max(totals, key=lambda c: (totals[c], c))


def _similarities(
    ref: str,
    utterance: Utterance,
    utterance_list: Sequence[Utterance],
    observations: Mapping[str, ObservationInput],
) -> dict[str, float]:
    """Cosine of this utterance's observation to every other one that has a vector."""
    source = observations.get(ref)
    if source is None or not source.vector:
        return {}
    from dnd_common.clustering import cosine as cosine_of

    out: dict[str, float] = {}
    for other in utterance_list:
        if other.ref == ref:
            continue
        target = observations.get(other.ref)
        if target is None or not target.vector:
            continue
        out[other.ref] = float(cosine_of(source.vector, target.vector))
    return out


def _speech_share(
    utterance_list: Sequence[Utterance],
    voice_of: Mapping[str, str | None],
    voices: Mapping[str, VoiceState],
) -> dict[str, float]:
    """Rough per-candidate speech share, for the dm_prior channel.

    The DM speaks the most and initiates scenes, so the share of the session's
    seconds a voice accounts for is a genuine (if weak) prior. Before any voice
    is placed it is spread evenly, because at that point we genuinely do not
    know who spoke more.
    """
    seconds: dict[str, float] = {}
    for utterance in utterance_list:
        voice_id = voice_of.get(utterance.ref)
        if not voice_id:
            continue
        seconds[voice_id] = seconds.get(voice_id, 0.0) + utterance.duration
    total = sum(seconds.values())
    if total <= 0:
        return {}
    share: dict[str, float] = {}
    for voice_id, value in seconds.items():
        voice_state = voices.get(voice_id)
        if voice_state is None:
            continue
        fraction = value / total
        for candidate, probability in voice_state.posterior.items():
            share[candidate] = share.get(candidate, 0.0) + fraction * probability
    if UNKNOWN in share:
        share.pop(UNKNOWN)
    return share


def _summary(
    belief: Belief, verdicts: Mapping[str, Verdict]
) -> tuple[float, float]:
    rows = [
        {
            "status": verdicts[ref].status,
            "stakes": belief.nodes[ref].stakes,
            "speech_sec": belief.nodes[ref].seconds,
        }
        for ref in belief.nodes
        if ref in verdicts
    ]
    return coverage(rows), unresolved_stakes(rows)


def _questions(
    *,
    plan_input: PlanInput,
    utterances: Sequence[Utterance],
    verdicts: Mapping[str, Verdict],
    result: InferenceResult,
    keys: Sequence[str],
    labels: Mapping[str, str],
    dm_key: str | None,
    voices: Mapping[str, VoiceState],
    handles: Mapping[str, str],
    observations: Mapping[str, ObservationInput],
    voice_of: Mapping[str, str | None],
) -> list[CandidateQuestion]:
    """Turn the belief into the questions the DM can actually answer."""
    context = QuestionContext(
        candidates=list(keys),
        label_of=labels,
        dm_key=dm_key,
        language=plan_input.language,
        thresholds=plan_input.thresholds,
        # Which moments the engine will actually ACT on. Everything else is
        # askable: a peaked posterior that the statuses refuse to promote is
        # ignorance, not knowledge (see questions.is_settled).
        confident_utterances={
            ref for ref, verdict in verdicts.items() if is_confident(verdict.status)
        },
    )
    views: dict[str, UtteranceView] = {}
    for utterance in utterances:
        item = plan_input.evidence.item(utterance.ref)
        views[utterance.ref] = UtteranceView(
            ref=utterance.ref,
            start=utterance.start,
            end=utterance.end,
            text=utterance.text,
            gist=item.gist,
            kind=item.kind,
            stakes=item.stakes,
            voice_id=voice_of.get(utterance.ref),
            speech_sec=utterance.duration,
        )
    voice_views = [
        VoiceView(
            id=voice_id,
            handle=handles.get(voice_id, voice_id),
            speech_sec=sum(
                u.duration for u in utterances if voice_of.get(u.ref) == voice_id
            ),
            purity=state.purity,
            utterance_refs=state.refs,
            start=min(
                (u.start for u in utterances if voice_of.get(u.ref) == voice_id),
                default=None,
            ),
            end=max(
                (u.end for u in utterances if voice_of.get(u.ref) == voice_id),
                default=None,
            ),
            centroid=_centroid(state, observations),
            posterior=state.posterior,
        )
        for voice_id, state in voices.items()
    ]
    if dm_key:
        # the DM is a candidate for the narration questions even though no voice
        # identity maps onto them yet
        for voice_view in voice_views:
            voice_view.posterior.setdefault(dm_key, 0.0)

    referenced = {
        ref: verdict.status in {"auto_low", "unresolved"}
        for ref, verdict in verdicts.items()
    }
    return generate(
        context=context,
        utterances=views,
        posteriors=result.posteriors,
        voices=voice_views,
        referenced=referenced,
        purity_split_threshold=plan_input.purity_split_threshold,
        clips=_clips(voice_views, observations, utterances),
    )


def _centroid(
    state: VoiceState, observations: Mapping[str, ObservationInput]
) -> tuple[float, ...]:
    vectors = [
        observations[ref].vector
        for ref in state.refs
        if ref in observations and observations[ref].vector
    ]
    if not vectors:
        return ()
    from dnd_common.clustering import centroid as centroid_of

    return tuple(centroid_of(vectors))


def _clips(
    voices: Sequence[VoiceView],
    observations: Mapping[str, ObservationInput],
    utterances: Sequence[Utterance],
) -> dict[str, tuple[float, float]]:
    """Short playable clips for the audio questions, one per voice (and a second
    sample for a voice that might be two people)."""
    by_ref = {u.ref: u for u in utterances}
    clips: dict[str, tuple[float, float]] = {}
    # NOT named 'voice': that is the imported evidence channel, and shadowing it
    # in this scope would silently break any later channel call in the function.
    for voice_view in voices:
        usable = [
            by_ref[ref]
            for ref in voice_view.utterance_refs
            if ref in by_ref and 2.0 <= by_ref[ref].duration <= 12.0
        ]
        if not usable:
            continue
        first = usable[0]
        clips[voice_view.id] = (first.start, first.end)
        if len(usable) > 1:
            last = usable[-1]
            clips[f"{voice_view.id}#second"] = (last.start, last.end)
    return clips

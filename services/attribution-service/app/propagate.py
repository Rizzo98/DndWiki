"""One answer is not a local patch: it is a global update.

docs/attribution-model.md S10. When the DM answers a question the engine:

1. clamps the answered utterance (or the whole voice identity);
2. treats the answer as a new voice-model centroid, so EVERY utterance in the
   session is re-scored against it;
3. learns any capability the answered utterance implied, which resolves every
   other utterance that requires it;
4. re-scopes the structure when the answer was a split/merge decision;
5. re-runs belief propagation and re-classifies;
6. measures what actually changed, which is the number the DM sees
   ("this also resolved 12 other moments").

`Propagator.simulate` runs exactly this, on a copy, without touching anything.
The question ranking calls it to decide what to ask, and the answer path calls
it to do it for real - which is why "answer about 2 questions" is honest by
construction rather than a guess (S9.4).

Pure module: it owns no database and publishes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.inference import Edge, Graph, InferenceResult, infer
from app.questions import IDK_OPTION, STRUCTURAL_KINDS, CandidateQuestion
from app.statuses import StatusThresholds, Verdict, classify, coverage

#: How strongly one answered utterance pulls another that sounds like it. Not a
#: clamp: the DM's answer is about a moment, and the generalisation to other
#: moments is an inference that must remain defeasible.
SAME_VOICE_PROPAGATION_LR = 2.5
#: A voice identity answer concentrates the whole identity, so it pulls its
#: members much harder than a single utterance answer.
VOICE_ANSWER_LR = 4.5
#: Sharing a learned capability is weaker evidence than sounding identical.
CAPABILITY_PROPAGATION_LR = 1.2
#: Similarity at which another observation is treated as the answered voice.
SIMILARITY_FLOOR = 0.7
#: The log of channels.ABSENT_PRIOR_FACTOR, in the form the potentials use. A
#: presence answer has to move a member by exactly what the absence evidence
#: moved them by, in one direction or the other, which is why the two constants
#: are defined next to each other: -log(0.05) ~ +3.0 nats.
ABSENT_PRIOR_FACTOR_LR = 3.0


@dataclass(frozen=True)
class Node:
    """One utterance, as propagation needs it."""

    ref: str
    voice_id: str | None = None
    stakes: float = 0.5
    seconds: float = 0.0
    capability_reqs: tuple[str, ...] = ()
    text: str = ""
    #: cosine of this utterance's observation to its identity's centroid
    alpha: float = 1.0
    #: similarity to every OTHER observation's embedding, when available
    similarity: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceState:
    """One voice identity, as propagation needs it."""

    id: str
    refs: tuple[str, ...] = ()
    posterior: dict[str, float] = field(default_factory=dict)
    #: None when the identity's purity was never MEASURED. That is not the same
    #: as 1.0, and the same_voice coupling depends on the difference.
    purity: float | None = None


@dataclass
class Belief:
    """The state the propagator transforms. Copied, never mutated in place."""

    candidates: list[str]
    nodes: dict[str, Node]
    #: node -> log phi from the channels (the answer is NOT included here)
    potentials: dict[str, dict[str, float]]
    edges: list[Edge] = field(default_factory=list)
    voices: dict[str, VoiceState] = field(default_factory=dict)
    #: node -> the strongest non-voice channel that supported the best candidate
    corroborating: dict[str, str | None] = field(default_factory=dict)
    #: node -> the voice channel's log LR for the best candidate
    voice_lr: dict[str, float] = field(default_factory=dict)
    thresholds: StatusThresholds = field(default_factory=StatusThresholds)
    #: node -> candidate, for utterances the DM already answered
    answers: dict[str, str] = field(default_factory=dict)
    #: voice id -> candidate, for voices the DM already placed
    voice_answers: dict[str, str] = field(default_factory=dict)
    #: candidate -> capabilities learned from answers
    learned: dict[str, set[str]] = field(default_factory=dict)
    #: voice ids the DM said were several people / the same person
    split_voices: set[str] = field(default_factory=set)
    merged_voices: dict[str, str] = field(default_factory=dict)
    #: Refs an ANSWER actually moved (S10). A moment nobody answered and nothing
    #: moved keeps its own observed status: it is not "inferred" just because
    #: some other utterance was answered, and marking it so demoted every
    #: auto_high node in the session onto the stricter propagated bar the moment
    #: the DM answered anything - which is how "we identified 15%" went DOWN to
    #: 14.7% after eight correct answers.
    #:
    #: None means "not recorded", which is what a snapshot written before the set
    #: existed looks like once it has answers in it. We then know an answer moved
    #: SOMETHING and not what, so every node falls back to the stricter bar
    #: rather than being promoted to `auto_high` on an observation nobody made.
    propagated_refs: set[str] | None = None

    def copy(self) -> Belief:
        return replace(
            self,
            nodes=dict(self.nodes),
            potentials={k: dict(v) for k, v in self.potentials.items()},
            edges=list(self.edges),
            voices=dict(self.voices),
            corroborating=dict(self.corroborating),
            voice_lr=dict(self.voice_lr),
            answers=dict(self.answers),
            voice_answers=dict(self.voice_answers),
            learned={k: set(v) for k, v in self.learned.items()},
            split_voices=set(self.split_voices),
            merged_voices=dict(self.merged_voices),
            propagated_refs=(
                None if self.propagated_refs is None else set(self.propagated_refs)
            ),
        )

    def effective_voice_of(self, ref: str) -> str | None:
        """The voice identity of an utterance, honouring merges and splits."""
        node = self.nodes.get(ref)
        if node is None or node.voice_id is None:
            return None
        if node.voice_id in self.split_voices:
            # a split destroys the pooled belief: every utterance stands alone
            return None
        return self.merged_voices.get(node.voice_id, node.voice_id)


@dataclass(frozen=True)
class PropagationOutcome:
    """What one answer actually changed (the payload of propagation_events)."""

    resolved: tuple[str, ...]
    resolved_seconds: float
    voice_ids: tuple[str, ...]
    learned_capabilities: tuple[str, ...]
    learned_voice_models: int
    coverage_before: float
    coverage_after: float
    contradiction: bool = False
    #: The moments this answer MOVED, the answered one excluded. They are the
    #: only ones that become `propagated` (see Belief.propagated_refs).
    moved: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "resolved_utterances": len(self.resolved),
            "resolved_sec": round(self.resolved_seconds, 2),
            "voice_ids": list(self.voice_ids),
            "learned": {
                "capabilities": len(self.learned_capabilities),
                "voice_centroids": self.learned_voice_models,
            },
            "coverage_before": round(self.coverage_before, 4),
            "coverage_after": round(self.coverage_after, 4),
            "contradiction": self.contradiction,
        }


def _add(potential: dict[str, float], candidate: str, value: float) -> None:
    potential[candidate] = potential.get(candidate, 0.0) + value


class Propagator:
    """Applies an answer to a belief, and can simulate one without applying it."""

    def __init__(self, belief: Belief) -> None:
        self.belief = belief

    # -- reading ------------------------------------------------------------

    def graph_for(self, belief: Belief) -> Graph:
        """The utterance graph implied by a belief (answers included)."""
        potentials = {ref: dict(values) for ref, values in belief.potentials.items()}
        for ref, candidate in belief.answers.items():
            if ref in potentials and candidate != IDK_OPTION:
                _add(potentials[ref], candidate, 8.0)
        for ref, values in potentials.items():
            voice = belief.effective_voice_of(ref)
            if voice is None:
                continue
            answer = belief.voice_answers.get(voice)
            if answer and answer != IDK_OPTION:
                _add(values, answer, VOICE_ANSWER_LR)
        for ref, values in potentials.items():
            _add_learned(values, belief, ref)
        return Graph(
            nodes=list(potentials),
            candidates=list(belief.candidates),
            log_potentials=potentials,
            edges=list(belief.edges),
        )

    def infer(self, belief: Belief) -> InferenceResult:
        return infer(self.graph_for(belief))

    def verdicts(
        self, belief: Belief, result: InferenceResult | None = None
    ) -> dict[str, Verdict]:
        result = result or self.infer(belief)
        out: dict[str, Verdict] = {}
        for ref, posterior in result.posteriors.items():
            values = sorted(posterior.values(), reverse=True)
            top = values[0] if values else 0.0
            second = values[1] if len(values) > 1 else 0.0
            from app.inference import entropy

            answered = ref in belief.answers or (
                belief.effective_voice_of(ref) in belief.voice_answers
            )
            propagated = ref not in belief.answers and self._was_propagated(belief, ref)
            out[ref] = classify(
                p_max=top,
                margin=top - second,
                entropy=entropy(posterior),
                corroborating_channel=belief.corroborating.get(ref),
                voice_lr=belief.voice_lr.get(ref, 0.0),
                decided_by=(
                    "question"
                    if answered
                    else ("propagation" if propagated else "engine")
                ),
                answered=answered,
                propagated=propagated,
                thresholds=belief.thresholds,
            )
        return out

    def _was_propagated(self, belief: Belief, ref: str) -> bool:
        """True when THIS utterance's belief moved because of someone else's answer.

        The set is written by measure() when the answer is applied, by comparing
        the posteriors on either side of it. It used to be "any answer exists
        anywhere", which is a different statement entirely: it re-labelled every
        untouched moment of the session as inferred, and since `propagated` is
        deliberately STRICTER than auto_high (S7.1), answering a question could
        only ever push the session's confidence DOWN.
        """
        if belief.propagated_refs is None:
            return bool(belief.answers or belief.voice_answers or belief.learned)
        return ref in belief.propagated_refs

    def coverage(self, belief: Belief, verdicts: Mapping[str, Verdict] | None = None) -> float:
        verdicts = verdicts or self.verdicts(belief)
        rows = [
            {
                "status": verdicts[ref].status,
                "stakes": belief.nodes[ref].stakes,
                "speech_sec": belief.nodes[ref].seconds,
            }
            for ref in belief.nodes
            if ref in verdicts
        ]
        return coverage(rows)

    # -- the answer ---------------------------------------------------------

    def apply(
        self, question: CandidateQuestion, option: str
    ) -> tuple[Belief, Belief]:
        """Return (belief before, belief after) for one answer.

        *I don't know* changes nothing except the record: the engine must be
        able to accept "I can't tell" without inventing a label (S8.3), so the
        answer is stored and the belief is left alone.
        """
        before = self.belief
        after = before.copy()
        if option == IDK_OPTION:
            return before, after

        kind = question.kind
        if kind in STRUCTURAL_KINDS:
            self._apply_structural(after, question, option)
        else:
            self._apply_content(after, question, option)
        return before, after

    def _apply_content(
        self, belief: Belief, question: CandidateQuestion, candidate: str
    ) -> None:
        """Clamp the answered moment and generalise from it (S10.1-S10.3)."""
        for ref in question.target_utterances:
            belief.answers[ref] = candidate
            # 10.2: the answered observation becomes a new centroid for the
            # member, so every utterance that SOUNDS like it is re-scored at
            # once. This is what produces "this also resolved 12 moments".
            source = belief.nodes.get(ref)
            if source is None:
                continue
            for other_ref in belief.nodes:
                if other_ref == ref or other_ref in belief.answers:
                    continue
                similarity = source.similarity.get(other_ref, 0.0)
                if similarity >= SIMILARITY_FLOOR:
                    _add(
                        belief.potentials.setdefault(other_ref, {}),
                        candidate,
                        SAME_VOICE_PROPAGATION_LR * similarity,
                    )
            # 10.3: remember what the moment proves the member can do
            if source.capability_reqs:
                belief.learned.setdefault(candidate, set()).update(source.capability_reqs)

    def _apply_structural(
        self, belief: Belief, question: CandidateQuestion, option: str
    ) -> None:
        """Split or merge (S10.4)."""
        if question.kind in {"same_voice", "different_voice"}:
            if option == "different":
                for voice_id in question.target_voices:
                    belief.split_voices.add(voice_id)
            elif option == "same":
                self._merge(belief, question.target_voices)
            return
        # who_is_voice / new_person place (or refuse to place) a whole identity
        for voice_id in question.target_voices:
            belief.voice_answers[voice_id] = option
            voice = belief.voices.get(voice_id)
            if voice is None:
                continue
            for ref in voice.refs:
                if option not in {IDK_OPTION}:
                    node = belief.nodes.get(ref)
                    if node is not None and node.capability_reqs:
                        belief.learned.setdefault(option, set()).update(
                            node.capability_reqs
                        )

    def _merge(self, belief: Belief, voice_ids: Sequence[str]) -> None:
        if len(voice_ids) < 2:
            return
        target, *others = voice_ids
        for other in others:
            belief.merged_voices[other] = target

    # -- simulation ---------------------------------------------------------

    def applied(self, question: CandidateQuestion, option: str) -> Propagator:
        """The propagator that WOULD follow this answer, from the current belief.

        Never mutates this one: only the DM's real answers move the belief
        (ReviewSession.answer), and a plan is a hypothesis about answers nobody
        has given yet. The plan needs the resulting STATE and not just its
        posteriors, because its next step is simulated from it.
        """
        _, after = self.apply(question, option)
        return Propagator(after)

    def simulate(
        self, question: CandidateQuestion, option: str
    ) -> dict[str, dict[str, float]]:
        """The posteriors that WOULD follow this answer (used by the ranking)."""
        following = self.applied(question, option)
        return following.infer(following.belief).posteriors

    # -- measuring ----------------------------------------------------------

    def measure(
        self, before: Belief, after: Belief, *, question_id: str | None = None
    ) -> PropagationOutcome:
        """What changed, in the numbers the DM is shown (S10.6).

        `resolved` counts utterances that moved INTO a confident status, not
        utterances that merely moved: a belief that shuffles probability around
        without deciding anything has not helped anybody.
        """
        from app.statuses import is_confident

        # Infer once on each side: the movement of every moment is what decides
        # which of them count as inferred, and the verdicts below are read off
        # the same two results instead of a third and fourth inference.
        result_before = self.infer(before)
        result_after = self.infer(after)
        moved = tuple(
            sorted(
                ref
                for ref, posterior in result_after.posteriors.items()
                if ref not in after.answers and _moved(result_before.posteriors.get(ref), posterior)
            )
        )
        # The belief that will become the propagator's state carries the record:
        # 'propagated' is a property of the moment, not of the run. `moved` is
        # known from here on, so an unknown set becomes a known one.
        after.propagated_refs = set(after.propagated_refs or ()) | set(moved)

        verdicts_before = self.verdicts(before, result_before)
        verdicts_after = self.verdicts(after, result_after)
        resolved: list[str] = []
        seconds = 0.0
        for ref, verdict in verdicts_after.items():
            if not is_confident(verdict.status):
                continue
            previous = verdicts_before.get(ref)
            if previous is not None and is_confident(previous.status):
                continue
            resolved.append(ref)
            seconds += after.nodes[ref].seconds if ref in after.nodes else 0.0
        voice_ids = tuple(
            sorted(
                {
                    after.effective_voice_of(ref) or ""
                    for ref in resolved
                    if after.effective_voice_of(ref)
                }
            )
        )
        learned = sorted(
            {cap for caps in after.learned.values() for cap in caps}
            - {cap for caps in before.learned.values() for cap in caps}
        )
        return PropagationOutcome(
            resolved=tuple(resolved),
            resolved_seconds=seconds,
            voice_ids=voice_ids,
            moved=moved,
            learned_capabilities=tuple(learned),
            learned_voice_models=len(after.voice_answers) - len(before.voice_answers),
            coverage_before=self.coverage(before, verdicts_before),
            coverage_after=self.coverage(after, verdicts_after),
        )


#: How much a posterior has to move for an answer to have MOVED that moment.
#: Not zero: inference is iterative, so untouched moments drift by tiny amounts,
#: and calling that "inferred from your answer" would put the session back where
#: it started.
MOVED_TOLERANCE = 1e-3


def _moved(
    before: Mapping[str, float] | None, after: Mapping[str, float]
) -> bool:
    """Whether an answer changed what we believe about this moment."""
    if before is None:
        return False
    keys = set(before) | set(after)
    return max(
        (abs(float(after.get(k, 0.0)) - float(before.get(k, 0.0))) for k in keys),
        default=0.0,
    ) >= MOVED_TOLERANCE


def moved_by_answers(belief: Belief) -> set[str]:
    """The moments that OWE their belief to the DM's answers (see _was_propagated).

    'measure' answers this for one answer as it is applied, by comparing the two
    sides of it. This answers it for the whole session at once, for a belief
    whose record of that was lost: it asks the same question by taking EVERY
    answer away and seeing which posteriors move. One extra inference, and it
    names exactly the moments the DM's answers are currently responsible for.

    It is the weaker claim of the two - a moment that only moved in the presence
    of a later answer is not in the set - and that is the point: the alternative
    used to be "somebody answered something, so every moment of the session is
    inferred", which demoted the whole session onto the stricter bar once the DM
    answered anything at all (S7.1).
    """
    if not (belief.answers or belief.voice_answers):
        return set()
    propagator = Propagator(belief)
    answered = set(belief.answers)
    without = belief.copy()
    without.answers = {}
    without.voice_answers = {}
    without.propagated_refs = set()
    before = propagator.infer(without).posteriors
    after = propagator.infer(belief).posteriors
    return {
        ref
        for ref, posterior in after.items()
        if ref not in answered and _moved(before.get(ref), posterior)
    }


def _add_learned(potential: dict[str, float], belief: Belief, ref: str) -> None:
    """Capability generalisation, applied at inference time.

    Learning is per (member, capability) and campaign-scoped, so "Player 3 cast
    Fireball once" permanently biases every utterance that requires Fireball -
    including in later sessions (S10.3).
    """
    if not belief.learned:
        return
    node = belief.nodes.get(ref)
    if node is None or not node.capability_reqs:
        return
    for candidate, capabilities in belief.learned.items():
        for requirement in node.capability_reqs:
            if requirement in capabilities:
                _add(potential, candidate, CAPABILITY_PROPAGATION_LR)


def contradiction(
    belief: Belief, *, ref: str, candidate: str, voice_cosines: Mapping[str, float]
) -> bool:
    """Whether an answer contradicts overwhelming voice evidence (S10.7).

    When it does, the answer is honoured FOR THAT UTTERANCE - a DM can be right
    about a guest using someone else's headset - but the voice model must not be
    rewritten globally, and the conflict is recorded. Silently averaging the two
    would destroy both.
    """
    best_other = max(
        (
            value
            for other, value in voice_cosines.items()
            if other != candidate
        ),
        default=0.0,
    )
    chosen = voice_cosines.get(candidate, 0.0)
    return best_other - chosen > 0.25 and best_other >= 0.75

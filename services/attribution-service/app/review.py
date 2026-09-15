"""The review loop, and the rule that says when to stop asking.

docs/attribution-model.md S11. The stopping criterion is the part that decides
whether the DM experience is good, so it is a function with tests rather than a
condition buried in a worker:

  stop when
    (1) no remaining question is worth the DM's time (G(q) < GAIN_FLOOR for all), AND
    (2) the unresolved stakes mass is below TARGET_UNRESOLVED AND every extracted
        event that matters has an attributed actor,
  or
    (3) the budget is spent, the DM said *I don't know* twice in a row, or the DM
        pressed Finish.

Condition (2) is the important one. Coverage is a SUMMARY; the stopping rule is
driven by what the wiki will CONSUME. A session is not done while the moments
that will become pages are still unattributed, even if the aggregate number
looks good - and it IS done the moment the only thing left is filler, even if
the aggregate looks mediocre.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.propagate import Belief, PropagationOutcome, Propagator
from app.questions import IDK_OPTION, CandidateQuestion
from app.ranking import (
    DEFAULT_SIMULATED_OPTIONS,
    DEFAULT_SIMULATION_BUDGET,
    PlannedReview,
    PlanSimulator,
    ScoredQuestion,
    plan_review,
    rank,
)
from app.statuses import Verdict, is_confident, unresolved_stakes

#: Reasons a review can stop. Persisted on review_runs.stop_reason and shown to
#: the DM, because "why did it stop asking me?" must have an answer.
STOP_REASONS: tuple[str, ...] = (
    "gain_floor",
    "target_reached",
    "budget",
    "repeated_dont_know",
    "finished",
    "no_candidates",
    "review_complete",
)


@dataclass(frozen=True)
class ReviewPolicy:
    """The knobs of S15.5, bundled so the rule is testable on its own."""

    gain_floor_bits: float = 0.15
    target_unresolved: float = 0.10
    max_questions: int = 8
    event_stakes_threshold: float = 0.7
    max_consecutive_idk: int = 2
    uninformative_penalty: float = 1.0
    overlap_penalty: float = 0.5
    idk_probability: float = 0.15
    #: How many candidates get the real (simulated) gain, and how many options
    #: per candidate. Both exist because the objective is MEASURED, not
    #: estimated, and a four-hour session offers hundreds of candidates: this
    #: is the knob that decides how long the ranking may take. See
    #: app.ranking.shortlist for exactly what is given up.
    simulation_budget: int = DEFAULT_SIMULATION_BUDGET
    max_simulated_options: int = DEFAULT_SIMULATED_OPTIONS


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {"stop": self.stop, "reason": self.reason, "detail": self.detail}


def should_stop(
    *,
    policy: ReviewPolicy,
    best_gain: float,
    unresolved: float,
    unattributed_events: Sequence[str] = (),
) -> StopDecision:
    """Conditions (1) and (2): the engine's own view of whether to keep asking."""
    if best_gain < policy.gain_floor_bits:
        return StopDecision(
            True, "gain_floor", {"best_gain": round(best_gain, 6)}
        )
    blocking = [
        ref for ref in unattributed_events
    ]
    if unresolved <= policy.target_unresolved and not blocking:
        return StopDecision(
            True,
            "target_reached",
            {
                "unresolved": round(unresolved, 4),
                "target": policy.target_unresolved,
            },
        )
    return StopDecision(
        False,
        "",
        {
            "unresolved": round(unresolved, 4),
            "unattributed_events": len(blocking),
        },
    )


def high_stakes_without_actor(
    belief: Belief,
    verdicts: Mapping[str, Verdict],
    *,
    threshold: float,
) -> list[str]:
    """Utterances important enough that an unattributed actor blocks the stop.

    These are the moments the wiki will turn into pages and events. Letting the
    review end while one of them is unresolved would be exactly the silent
    failure the redesign exists to prevent.
    """
    return [
        ref
        for ref, node in belief.nodes.items()
        if node.stakes >= threshold
        and not is_confident(verdicts[ref].status)
    ]


@dataclass
class ReviewOutcome:
    """Everything the review produced, ready for the API and the events."""

    planned: PlannedReview
    asked: list[ScoredQuestion] = field(default_factory=list)
    outcome: str = "in_progress"
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    unresolved_after: float = 0.0
    stop_reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "questions_asked": len(self.asked),
            "questions_planned": self.planned.planned,
            "coverage_before": round(self.coverage_before, 4),
            "coverage_after": round(self.coverage_after, 4),
            "unresolved_after": round(self.unresolved_after, 4),
            "stop_reason": self.stop_reason,
        }


def _plan_simulator(propagator: Propagator) -> PlanSimulator:
    """The state-aware simulator the greedy plan needs (ranking.PlanSimulator).

    One simulated answer produces the belief the NEXT simulated answer has to be
    applied to, and that belief is the opaque token the caller carries. Without
    it the plan simulates every step from the belief the plan started from while
    measuring against a baseline that has moved on - which is not an
    approximation but a category error, and reports "1 question" for a review
    that will keep asking for eight.
    """

    def simulate(
        question: CandidateQuestion, option: str, base: Any
    ) -> tuple[Propagator, Mapping[str, Mapping[str, float]]]:
        # 'base' is the propagator a previous simulated answer produced, and
        # None means "the belief the plan was asked about".
        source = propagator if base is None else base
        following = source.applied(question, option)
        return following, following.infer(following.belief).posteriors

    return simulate


class ReviewSession:
    """One session's review: the questions, the state, and the stopping rule.

    The engine side of the review card. It never writes anything itself: it
    answers questions about what to ask next, what an answer would change, and
    whether the review is over. The persistence lives in the worker/API.
    """

    def __init__(
        self,
        *,
        propagator: Propagator,
        questions: Sequence[CandidateQuestion],
        policy: ReviewPolicy | None = None,
        uninformative_rates: Mapping[str, float] | None = None,
    ) -> None:
        self.propagator = propagator
        self.policy = policy or ReviewPolicy()
        self.uninformative_rates = dict(uninformative_rates or {})
        self.questions = list(questions)
        self.asked: list[ScoredQuestion] = []
        self.answered_refs: set[str] = set()
        self.covered_voices: set[str] = set()
        self.consecutive_idk = 0
        self.finished = False
        self.initial_coverage = self._coverage()

    # -- reading ------------------------------------------------------------

    def belief(self) -> Belief:
        return self.propagator.belief

    def _verdicts(self, belief: Belief | None = None) -> dict[str, Verdict]:
        return self.propagator.verdicts(belief or self.belief())

    def _coverage(self, belief: Belief | None = None) -> float:
        return self.propagator.coverage(belief or self.belief())

    def unresolved(self, belief: Belief | None = None) -> float:
        belief = belief or self.belief()
        verdicts = self._verdicts(belief)
        rows = [
            {
                "status": verdicts[ref].status,
                "stakes": node.stakes,
                "speech_sec": node.seconds,
            }
            for ref, node in belief.nodes.items()
            if ref in verdicts
        ]
        return unresolved_stakes(rows)

    def status(self, questions_planned: int | None = None) -> dict[str, Any]:
        """What the review card shows.

        'questions_planned' is PASSED IN whenever the caller has it stored: it
        is the result of the most expensive computation in the system (the
        greedy simulation), and recomputing it to display a number that was
        already persisted is what made loading the session page cost minutes.
        The argument is optional so a caller without a stored value - the
        engine computing a fresh plan - still gets the honest number.
        """
        belief = self.belief()
        verdicts = self._verdicts(belief)
        buckets: dict[str, int] = {}
        for verdict in verdicts.values():
            buckets[verdict.status] = buckets.get(verdict.status, 0) + 1
        if questions_planned is None:
            questions_planned = self.plan().planned
        return {
            "coverage": round(self._coverage(belief), 4),
            "unresolved": round(self.unresolved(belief), 4),
            "buckets": buckets,
            "questions_asked": len(self.asked),
            "questions_planned": questions_planned,
            "finished": self.finished,
        }

    # -- the question ------------------------------------------------------

    def ranked(self) -> list[ScoredQuestion]:
        from app.inference import global_entropy

        belief = self.belief()
        stakes = {ref: node.stakes for ref, node in belief.nodes.items()}
        covered = set(self.covered_voices) | set(self.answered_refs)
        return rank(
            self.remaining(),
            baseline_entropy=global_entropy(
                self.propagator.infer(belief).posteriors, stakes
            ),
            posteriors=self.propagator.infer(belief).posteriors,
            simulate=self.propagator.simulate,
            idk_probability=self.policy.idk_probability,
            uninformative_rates=self.uninformative_rates,
            covered=covered,
            uninformative_penalty=self.policy.uninformative_penalty,
            overlap_penalty=self.policy.overlap_penalty,
            simulation_budget=self.policy.simulation_budget,
            max_simulated_options=self.policy.max_simulated_options,
            # the SAME stakes the baseline above was measured with
            stakes=stakes,
        )

    def remaining(self) -> list[CandidateQuestion]:
        asked_texts = {scored.question.prompt_text for scored in self.asked}
        return [q for q in self.questions if q.prompt_text not in asked_texts]

    def next_question(self) -> tuple[ScoredQuestion | None, StopDecision]:
        """The top-ranked question, or a stop decision explaining why there is none."""
        if self.finished:
            return None, StopDecision(True, "finished")
        if len(self.asked) >= self.policy.max_questions:
            return None, StopDecision(True, "budget", {"asked": len(self.asked)})
        if self.consecutive_idk >= self.policy.max_consecutive_idk:
            return None, StopDecision(True, "repeated_dont_know")

        ranked = self.ranked()
        if not ranked:
            return None, StopDecision(True, "no_candidates")
        best = ranked[0]
        belief = self.belief()
        verdicts = self._verdicts(belief)
        decision = should_stop(
            policy=self.policy,
            best_gain=best.expected_gain,
            unresolved=self.unresolved(belief),
            unattributed_events=high_stakes_without_actor(
                belief, verdicts, threshold=self.policy.event_stakes_threshold
            ),
        )
        if decision.stop:
            return None, decision
        return best, StopDecision(False, "")

    def plan(self) -> PlannedReview:
        """The greedy simulation behind "answer about N questions"."""
        belief = self.belief()
        stakes = {ref: node.stakes for ref, node in belief.nodes.items()}
        return plan_review(
            questions=self.remaining(),
            posteriors=self.propagator.infer(belief).posteriors,
            stakes=stakes,
            simulate=_plan_simulator(self.propagator),
            max_questions=self.policy.max_questions,
            gain_floor=self.policy.gain_floor_bits,
            idk_probability=self.policy.idk_probability,
            uninformative_rates=self.uninformative_rates,
            is_done=lambda posteriors: self._simulated_done(posteriors, stakes),
            simulation_budget=self.policy.simulation_budget,
            max_simulated_options=self.policy.max_simulated_options,
        )

    def _simulated_done(
        self, posteriors: Mapping[str, Mapping[str, float]], stakes: Mapping[str, float]
    ) -> bool:
        rows = [
            {
                "status": "auto_high" if max(p.values(), default=0) >= 0.9 else "auto_low",
                "stakes": stakes.get(ref, 0.5),
                "speech_sec": 1.0,
            }
            for ref, p in posteriors.items()
        ]
        return unresolved_stakes(rows) < self.policy.target_unresolved

    # -- answering ---------------------------------------------------------

    def answer(self, scored: ScoredQuestion, option: str) -> PropagationOutcome:
        """Apply one answer and record what it resolved."""
        question = scored.question
        before = self.belief()
        after = self.propagator.apply(question, option)[1]
        outcome = self.propagator.measure(before, after)
        self.propagator.belief = after
        self.asked.append(scored)
        if option == IDK_OPTION:
            self.consecutive_idk += 1
        else:
            self.consecutive_idk = 0
        for ref in question.target_utterances:
            self.answered_refs.add(ref)
        self.covered_voices.update(question.target_voices)
        return outcome

    def finish(self, *, reason: str = "finished") -> ReviewOutcome:
        """The DM pressed Finish, or the engine reported nothing left to ask."""
        self.finished = True
        return ReviewOutcome(
            planned=self.plan(),
            asked=list(self.asked),
            outcome="finished" if reason == "finished" else reason,
            coverage_before=self.initial_coverage,
            coverage_after=self._coverage(),
            unresolved_after=self.unresolved(),
            stop_reason=reason,
        )

    def outcome(self, *, reason: str) -> ReviewOutcome:
        return ReviewOutcome(
            planned=self.plan(),
            asked=list(self.asked),
            outcome=reason,
            coverage_before=self.initial_coverage,
            coverage_after=self._coverage(),
            unresolved_after=self.unresolved(),
            stop_reason=reason,
        )


def report(verdicts: Mapping[str, Verdict]) -> dict[str, Any]:
    """The bucket counts the review card shows, in status order."""
    buckets: dict[str, int] = {}
    for verdict in verdicts.values():
        buckets[verdict.status] = buckets.get(verdict.status, 0) + 1
    return {
        "buckets": buckets,
        "confident": sum(
            1 for verdict in verdicts.values() if is_confident(verdict.status)
        ),
        "total": len(verdicts),
    }


def top_candidates(verdict_counts: Mapping[str, int]) -> str:
    """A one-line summary for the logs ("3 auto_high, 41 unresolved")."""
    if not verdict_counts:
        return "nothing attributed"
    return ", ".join(
        f"{count} {status}" for status, count in sorted(verdict_counts.items())
    )

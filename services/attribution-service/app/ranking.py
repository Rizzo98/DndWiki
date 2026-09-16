"""Ranking: which question buys the most certainty for the least effort.

The objective is the expected reduction in the TOTAL uncertainty of the session
(docs/attribution-model.md S9.1):

    G(q) = H(X_U | E)  -  SUM_{a in A_q} P(a | E) * H(X_U | E, a)

and `H(X_U | E, a)` is NOT estimated from a formula: it is the entropy measured
after actually running the propagation for answer `a` and re-running belief
propagation. We ask the question whose SIMULATED answer reduces total
uncertainty the most - which is exactly the "resolve as many other moments as
possible" property the product needs.

That simulation is injected as a callback, so this module stays pure and
testable: a test can pass a fake simulator and check the arithmetic, and the
engine passes the real one.

Greedy selection is near-optimal here because the objective is monotone
submodular in the set of answered questions, so greedy achieves at least
1 - 1/e of the optimal expected gain. A cleverer search would not be
explainable to the DM anyway (S9.2).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.inference import entropy, global_entropy
from app.questions import (
    DEFAULT_IDK_PROBABILITY,
    IDK_OPTION,
    STRUCTURAL_KINDS,
    CandidateQuestion,
)

#: simulate(question, option_key) -> the posteriors that would follow this answer
#: FROM THE CALLER'S CURRENT BELIEF. This is the right shape for "what would
#: answering this change?", which is asked about one question at a time.
Simulator = Callable[[CandidateQuestion, str], Mapping[str, Mapping[str, float]]]

#: The state-aware simulator a SEQUENCE of answers needs:
#:
#:     simulate(question, option_key, base) -> (next_base, posteriors)
#:
#: 'base' is opaque to this module: the simulator creates it and is handed it
#: back unchanged, and None means "the state the plan was asked about". It exists
#: because step two of a plan has to be simulated from the belief step one
#: produced. A single-state simulator cannot express that, and using one anyway
#: is not an approximation - it measures a posterior the simulation never
#: visited against a baseline it never had, so every step after the first comes
#: out negative, is clamped to zero, and the plan reports "1 question" for a
#: review that will keep asking for eight (see plan_review).
PlanSimulator = Callable[
    [CandidateQuestion, str, Any],
    tuple[Any, Mapping[str, Mapping[str, float]]],
]
#: How likely the DM is to pick each option when the model has no opinion.
UNIFORM_ANSWER_FLOOR = 1e-6

#: How many of the most probable options get a simulated answer. The expected
#: gain is an average over the options, so an option the DM is very unlikely to
#: pick moves it very little; measuring the top few and letting the rest be
#: covered by the gain floor is what keeps the ranking affordable.
DEFAULT_SIMULATED_OPTIONS = 3

#: How many candidate questions get the REAL simulation. Everything beyond this
#: is never asked. See rank().
DEFAULT_SIMULATION_BUDGET = 24


@dataclass(frozen=True)
class ScoredQuestion:
    """A candidate question with its expected gain and composite score."""

    question: CandidateQuestion
    expected_gain: float
    answer_probabilities: dict[str, float]
    entropy_after: dict[str, float]
    uninformative_penalty: float
    overlap_penalty: float
    score: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.question.kind,
            "prompt_text": self.question.prompt_text,
            "expected_gain": round(self.expected_gain, 6),
            "gain_detail": {
                "entropy_after": {
                    k: round(v, 6) for k, v in self.entropy_after.items()
                },
                "answer_probabilities": {
                    k: round(v, 6) for k, v in self.answer_probabilities.items()
                },
            },
            "score": round(self.score, 6),
            "cost": self.question.cost,
        }


def answer_probabilities(
    question: CandidateQuestion,
    *,
    posterior: Mapping[str, float] | None = None,
    idk_probability: float = DEFAULT_IDK_PROBABILITY,
) -> dict[str, float]:
    """P(a | E) for every option the DM could pick.

    Roster options take their probability from the belief, capped so that a
    confident model does not claim the DM is certain to agree; "I don't know"
    takes the campaign's measured rate (or the default), because a tired DM says
    it for reasons that have nothing to do with the evidence.
    """
    options = [option.key for option in question.options]
    if not options:
        return {}
    idk_probability = max(0.0, min(0.9, idk_probability))
    known = [key for key in options if key != IDK_OPTION]
    if not known:
        return {IDK_OPTION: 1.0}

    posterior = posterior or {}
    raw = {key: max(UNIFORM_ANSWER_FLOOR, float(posterior.get(key, 0.0))) for key in known}
    total = sum(raw.values())
    if total <= 0:
        raw = {key: 1.0 / len(known) for key in known}
    else:
        raw = {key: value / total for key, value in raw.items()}

    probabilities = {
        key: value * (1.0 - idk_probability) for key, value in raw.items()
    }
    probabilities[IDK_OPTION] = idk_probability
    total = sum(probabilities.values())
    return {key: value / total for key, value in probabilities.items()}


def expected_gain(
    *,
    baseline_entropy: float,
    entropy_after: Mapping[str, float],
    probabilities: Mapping[str, float],
) -> float:
    """G(q) in the units of the objective (stakes-weighted nats).

    Clamped at 0: a question that is expected to make things WORSE is not a
    question, it is noise, and the floor in S9.3 should see 0 rather than a
    negative number that then propagates into the stopping rule.
    """
    expected_after = sum(
        probabilities.get(option, 0.0) * value for option, value in entropy_after.items()
    )
    return max(0.0, float(baseline_entropy - expected_after))


def score(
    *,
    gain: float,
    mean_stakes: float,
    cost: float,
    uninformative: float,
    overlap: float,
    uninformative_penalty: float = 1.0,
    overlap_penalty: float = 0.5,
) -> float:
    """score(q) = G(q) * mean_stakes / cost - rho*P_uninformative - lambda*overlap.

    Every term is a product decision from S9.3: stakes keep the review pointed at
    what the wiki will use, cost prices the DM's effort, and the two penalties
    make the kinds the DM keeps bouncing decay instead of being re-asked.
    """
    base = gain * max(0.0, mean_stakes) / max(1e-6, cost)
    return float(base - uninformative_penalty * uninformative - overlap_penalty * overlap)


def score_question(
    question: CandidateQuestion,
    *,
    baseline_entropy: float,
    posterior: Mapping[str, float] | None,
    simulate: Simulator,
    idk_probability: float = DEFAULT_IDK_PROBABILITY,
    uninformative_rates: Mapping[str, float] | None = None,
    overlap: float = 0.0,
    uninformative_penalty: float = 1.0,
    overlap_penalty: float = 0.5,
    max_simulated_options: int = DEFAULT_SIMULATED_OPTIONS,
    stakes: Mapping[str, float] | None = None,
) -> ScoredQuestion:
    """Score one candidate by simulating the answers the DM could give.

    'stakes' MUST be the same weighting the caller used for 'baseline_entropy'.
    The objective is a DIFFERENCE of two entropies, so measuring one of them
    with stakes and the other without makes every gain negative - and negative
    gains are clamped to zero, which is indistinguishable from "there is nothing
    worth asking". That is precisely how this shipped: the baseline was
    stakes-weighted, the measurement was not, and the engine asked the DM
    nothing at all.

    Two things are NOT simulated, and neither is an approximation:

    - *I don't know*. It changes nothing except the record (S8.3), so the
      entropy after it IS the baseline entropy. Getting that for free removes a
      full propagation from every candidate.
    - the options too improbable to move the average. Those keep the baseline
      entropy, which is the conservative reading: it assumes they teach us
      nothing, so the question's measured gain is never talked up.
    """
    probabilities = answer_probabilities(
        question, posterior=posterior, idk_probability=idk_probability
    )
    # "I don't know" is dropped BEFORE the cut, not after: it is free to
    # account for, and it often carries a large share of the probability mass
    # (the DM's real rate is ~15%), so letting it take a slot would leave the
    # budget simulating one fewer real candidate.
    ranked_options = sorted(
        (
            (option, probability)
            for option, probability in probabilities.items()
            if option != IDK_OPTION
        ),
        key=lambda item: (-item[1], item[0]),
    )
    simulated = {option for option, _ in ranked_options[: max(1, max_simulated_options)]}
    entropy_after: dict[str, float] = {}
    for option in probabilities:
        if option in simulated:
            entropy_after[option] = global_entropy(
                simulate(question, option), stakes
            )
        else:
            entropy_after[option] = baseline_entropy
    gain = expected_gain(
        baseline_entropy=baseline_entropy,
        entropy_after=entropy_after,
        probabilities=probabilities,
    )
    rates = uninformative_rates or {}
    uninformative = float(rates.get(question.kind, 0.0))
    return ScoredQuestion(
        question=question,
        expected_gain=gain,
        answer_probabilities=probabilities,
        entropy_after=entropy_after,
        uninformative_penalty=uninformative,
        overlap_penalty=overlap,
        score=score(
            gain=gain,
            mean_stakes=question.mean_stakes,
            cost=question.cost,
            uninformative=uninformative,
            overlap=overlap,
            uninformative_penalty=uninformative_penalty,
            overlap_penalty=overlap_penalty,
        ),
    )


def shortlist(
    questions: Sequence[CandidateQuestion],
    *,
    posteriors: Mapping[str, Mapping[str, float]],
    max_candidates: int,
    stakes: Mapping[str, float] | None = None,
    priority: Collection[str] = (),
) -> list[CandidateQuestion]:
    """The candidates worth SIMULATING, best guess first.

    The real objective is measured, not estimated - it needs a propagation per
    option per candidate, and a four-hour session produces hundreds of
    candidates. So the shortlist is drawn with a cheap stand-in that uses the
    same ingredients minus the propagation: how much UNCERTAINTY THE QUESTION
    TOUCHES, in the objective's own units (stakes-weighted nats), divided by what
    the question costs the DM.

    "Touches" is a sum over the moments the question is about, and that is the
    correction: this used to be the entropy of their AVERAGE posterior, which is
    the same number whether the question covers one moment or a hundred. An
    answer that settles a whole voice identity therefore scored no better than
    one that settles a single line, and lost the tiebreak on cost and mean
    stakes. Measured on a real session: the seven `who_is_voice` questions
    ranked 121-129 of 131 against a simulation budget of 24 - the question kind
    with by far the largest knock-on effect in the system was never simulated,
    and the review asked eight single-moment questions instead (S9.3).

    What this CANNOT see is the knock-on effect an answer has on moments OUTSIDE
    the question, nor whether the answer settles its targets or merely reshapes
    them. So the proxy picks who competes; it never decides who wins, and it is
    never reported as a gain. The bound this buys is stated plainly rather than
    hidden: a question the proxy ranks below the budget is never asked.

    'priority' is the exception to that bound, and it exists because measuring
    the proxy against a real session found it starving the questions the review
    exists for. The proxy asks "how much uncertainty does this touch", which is
    large for a moment nobody can call and SMALL for a moment the engine already
    leans towards - so on that session the four questions about the beats the
    summary wrote as "un personaggio" ranked 37, 39, 40 and 78 of 156, and were
    never simulated at all. The moments the stopping rule is BLOCKED on are not a
    preference: they are the moments the wiki is still waiting for a name on
    (S11 condition 2). So they are shortlisted first, by the same proxy, and the
    simulation still decides whether any of them is worth asking.
    """
    if max_candidates <= 0 or len(questions) <= max_candidates:
        return list(questions)

    def proxy(question: CandidateQuestion) -> float:
        reach = _reach(question, posteriors, stakes=stakes)
        return reach / max(1e-6, question.cost)

    def order(items: Sequence[CandidateQuestion]) -> list[CandidateQuestion]:
        return sorted(items, key=lambda q: (-proxy(q), q.kind, q.prompt_text))

    wanted = set(priority)
    blocking = order([q for q in questions if wanted & set(q.target_utterances)])
    rest = order([q for q in questions if not (wanted & set(q.target_utterances))])
    return (blocking + rest)[:max_candidates]


def _reach(
    question: CandidateQuestion,
    posteriors: Mapping[str, Mapping[str, float]],
    *,
    stakes: Mapping[str, float] | None = None,
) -> float:
    """The uncertainty this question puts on the table, in the objective's units.

    Every kind is measured the same way, including the structural ones: a moment
    a question cannot reach is a moment it cannot be asked about, and silently
    scoring a whole KIND at zero - which is what "no target utterances" used to
    mean - is not a preference for content questions, it is a bug with a
    plausible-looking number.
    """
    refs = _target_refs(question)
    total = 0.0
    counted = 0
    for ref in refs:
        posterior = posteriors.get(ref)
        if not posterior:
            continue
        counted += 1
        weight = (
            float(stakes.get(ref, question.mean_stakes))
            if stakes
            else float(question.mean_stakes)
        )
        total += max(0.0, weight) * entropy(posterior)
    if counted:
        return total
    # Nothing measurable (no posterior for any target): fall back to the
    # question's own claim instead of to zero, so it still competes.
    pooled = _pooled_posterior(posteriors, refs)
    return max(0.0, question.mean_stakes) * (entropy(pooled) if pooled else 0.0)


def _target_refs(question: CandidateQuestion) -> list[str]:
    """The moments this question is about.

    Its own, and only its own. Every question carries the moments it is about,
    and adding a voice's cluster on top of that credited a single-line question
    with the reach of every moment that voice speaks, which is how the voice
    questions and the single-line ones kept swapping places on a number that
    meant nothing.
    """
    return list(dict.fromkeys(question.target_utterances))


def rank(
    questions: Sequence[CandidateQuestion],
    *,
    baseline_entropy: float,
    posteriors: Mapping[str, Mapping[str, float]],
    simulate: Simulator,
    idk_probability: float = DEFAULT_IDK_PROBABILITY,
    uninformative_rates: Mapping[str, float] | None = None,
    covered: set[str] | None = None,
    uninformative_penalty: float = 1.0,
    overlap_penalty: float = 0.5,
    limit: int | None = None,
    simulation_budget: int = DEFAULT_SIMULATION_BUDGET,
    max_simulated_options: int = DEFAULT_SIMULATED_OPTIONS,
    stakes: Mapping[str, float] | None = None,
    priority: Collection[str] = (),
) -> list[ScoredQuestion]:
    """Score the candidates worth scoring, best-first.

    Ties break on (kind, prompt_text) so a recompute over identical evidence
    reaches the identical order - a review that shuffles between page loads
    would be indistinguishable from a broken one.

    'stakes' and 'priority' exist here for the SHORTLIST: the proxy needs the
    stakes of the moments a question touches, and the moments the stopping rule
    is blocked on are shortlisted first (see shortlist and _reach). Neither is
    used by the scoring itself, which measures the real thing.
    """
    covered = covered or set()
    candidates = shortlist(
        questions,
        posteriors=posteriors,
        max_candidates=simulation_budget,
        stakes=stakes,
        priority=priority,
    )
    scored: list[ScoredQuestion] = []
    for question in candidates:
        posterior = None
        if question.target_utterances:
            posterior = _pooled_posterior(posteriors, question.target_utterances)
        overlap = _overlap(question, covered)
        scored.append(
            score_question(
                question,
                baseline_entropy=baseline_entropy,
                posterior=posterior,
                simulate=simulate,
                idk_probability=idk_probability,
                uninformative_rates=uninformative_rates,
                overlap=overlap,
                uninformative_penalty=uninformative_penalty,
                overlap_penalty=overlap_penalty,
                max_simulated_options=max_simulated_options,
                stakes=stakes,
            )
        )
    scored.sort(key=lambda s: (-s.score, s.question.kind, s.question.prompt_text))
    return scored[:limit] if limit is not None else scored


def _pooled_posterior(
    posteriors: Mapping[str, Mapping[str, float]], refs: Sequence[str]
) -> dict[str, float]:
    pooled: dict[str, float] = {}
    count = 0
    for ref in refs:
        posterior = posteriors.get(ref)
        if not posterior:
            continue
        count += 1
        for candidate, probability in posterior.items():
            pooled[candidate] = pooled.get(candidate, 0.0) + probability
    if count == 0:
        return {}
    return {candidate: value / count for candidate, value in pooled.items()}


def _overlap(question: CandidateQuestion, covered: set[str]) -> float:
    """Redundancy against what was already asked, on the question's OWN axis.

    Two axes live in one set and are looked up by the question's own kind:
    content questions are about moments, structural ones about voices. A
    structural question is asked about a cluster, and measuring it on the moments
    would let one of them suppress every moment that voice speaks.
    """
    targets = (
        set(question.target_voices)
        if question.kind in STRUCTURAL_KINDS
        else set(question.target_utterances)
    )
    if not targets:
        return 0.0
    return len(targets & covered) / len(targets)


@dataclass
class PlannedReview:
    """The greedy simulation: how many questions to reach the stopping rule.

    It is a BEST CASE and never a promise: it assumes the DM answers every
    question the way the evidence points, and it is a function of the state, so
    the same session gives 6 here and 8 there. It is kept as a diagnostic and as
    the reason a review exists; the card does not show it (S9.4).
    """

    planned: int
    questions: list[str] = field(default_factory=list)
    entropy_trace: list[float] = field(default_factory=list)
    stopped_because: str = "converged"

    def as_payload(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "entropy_trace": [round(v, 6) for v in self.entropy_trace],
            "stopped_because": self.stopped_because,
        }


def plan_review(
    *,
    questions: Sequence[CandidateQuestion],
    posteriors: Mapping[str, Mapping[str, float]],
    stakes: Mapping[str, float],
    simulate: PlanSimulator,
    max_questions: int = 8,
    gain_floor: float = 0.15,
    idk_probability: float = DEFAULT_IDK_PROBABILITY,
    uninformative_rates: Mapping[str, float] | None = None,
    is_done: Callable[[Mapping[str, Mapping[str, float]]], bool] | None = None,
    simulation_budget: int = DEFAULT_SIMULATION_BUDGET,
    max_simulated_options: int = DEFAULT_SIMULATED_OPTIONS,
    priority: Collection[str] = (),
) -> PlannedReview:
    """Greedily simulate the review under the current belief.

    At each step: score the remaining candidates FROM THE CURRENT STEP'S STATE,
    take the argmax OPTION (not the argmax answer - the DM is assumed to answer
    the way the evidence points), propagate, and stop when nothing clears the
    gain floor or the budget is spent. The result is the honest answer to "how
    many questions will this take me?".

    'simulate' is state-aware for exactly that reason: a plan is a SEQUENCE, so
    the step that is measured has to be the step that is simulated. See
    PlanSimulator.
    """
    current = dict(posteriors)
    remaining = list(questions)
    trace = [global_entropy(current, stakes)]
    asked: list[str] = []
    stopped = "converged"
    #: The belief this step simulates FROM (see PlanSimulator). Advanced with
    #: every simulated answer, so the baseline and the answers it is compared
    #: against are always the same belief.
    base: Any = None

    for _ in range(max(1, max_questions)):

        def branch(
            question: CandidateQuestion, option: str, _base: Any = base
        ) -> Mapping[str, Mapping[str, float]]:
            """Simulate one candidate answer from THIS STEP'S state.

            Scoring wants a one-answer simulator, and binding 'base' is what
            turns one into the other: every candidate is measured from the
            belief the previous answers would have produced, which is the belief
            the baseline below is computed on.
            """
            return simulate(question, option, _base)[1]

        ranked = rank(
            remaining,
            baseline_entropy=global_entropy(current, stakes),
            posteriors=current,
            simulate=branch,
            idk_probability=idk_probability,
            uninformative_rates=uninformative_rates,
            simulation_budget=simulation_budget,
            max_simulated_options=max_simulated_options,
            stakes=stakes,
            priority=priority,
        )
        if not ranked:
            stopped = "no_candidates"
            break
        best = ranked[0]
        if best.expected_gain < gain_floor:
            stopped = "gain_floor"
            break

        option = max(
            best.answer_probabilities,
            key=lambda key: (best.answer_probabilities[key], key),
        )
        if option == IDK_OPTION:
            option = max(
                (k for k in best.answer_probabilities if k != IDK_OPTION),
                key=lambda key: (best.answer_probabilities[key], key),
                default=IDK_OPTION,
            )
        # The simulation already ran for every option during scoring; re-run for
        # the chosen one to advance the state AND to carry the state forward.
        base, advanced = simulate(best.question, option, base)
        current = dict(advanced)
        asked.append(best.question.prompt_text)
        trace.append(global_entropy(current, stakes))
        remaining = [
            q for q in remaining if q.prompt_text != best.question.prompt_text
        ]
        if is_done is not None and is_done(current):
            stopped = "stopping_criterion"
            break
    else:
        stopped = "budget"

    return PlannedReview(
        planned=len(asked), questions=asked, entropy_trace=trace, stopped_because=stopped
    )


def entropy_of(posterior: Mapping[str, float]) -> float:
    """Convenience re-export so callers do not import app.inference directly."""
    return entropy(posterior)

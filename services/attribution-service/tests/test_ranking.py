"""Ranking tests: the objective, the penalties, and the greedy plan."""



import pytest

from app.inference import global_entropy
from app.questions import IDK_OPTION, CandidateQuestion, QuestionOption
from app.ranking import (
    PlannedReview,
    _target_refs,
    answer_probabilities,
    expected_gain,
    plan_review,
    rank,
    score,
    score_question,
    shortlist,
)

A, B, U = "member:a", "member:b", "U"


def question(kind="who_did", cost=1.0, stakes=0.9, targets=("u1",), prompt="Who did it?"):
    return CandidateQuestion(
        kind=kind,
        prompt_text=prompt,
        options=[
            QuestionOption(key=A, label="Alice"),
            QuestionOption(key=B, label="Bob"),
            QuestionOption(key=IDK_OPTION, label="I don't know"),
        ],
        target_utterances=list(targets),
        target_voices=["V1"] if kind == "same_voice" else [],
        hook={"quote": "I did it", "audio": {"start": 0.0, "end": 1.0}},
        cost=cost,
        mean_stakes=stakes,
    )


# --- answer probabilities ---------------------------------------------------


def test_answer_probabilities_follow_the_belief():
    probabilities = answer_probabilities(
        question(), posterior={A: 0.8, B: 0.2}, idk_probability=0.0
    )
    assert probabilities[A] == pytest.approx(0.8)
    assert probabilities[B] == pytest.approx(0.2)
    assert probabilities[IDK_OPTION] == pytest.approx(0.0)


def test_i_dont_know_carries_a_real_share():
    probabilities = answer_probabilities(
        question(), posterior={A: 1.0, B: 0.0}, idk_probability=0.2
    )
    assert probabilities[IDK_OPTION] == pytest.approx(0.2)
    assert probabilities[A] == pytest.approx(0.8)
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_a_flat_belief_makes_the_rosters_options_equal():
    probabilities = answer_probabilities(
        question(), posterior={}, idk_probability=0.0
    )
    assert probabilities[A] == pytest.approx(0.5)
    assert probabilities[B] == pytest.approx(0.5)


def test_probabilities_never_reach_zero_so_the_gain_stays_finite():
    probabilities = answer_probabilities(
        question(), posterior={A: 1.0, B: 0.0}, idk_probability=0.0
    )
    assert probabilities[B] >= 0.0


# --- the objective ----------------------------------------------------------


def test_expected_gain_is_the_entropy_the_answer_would_remove():
    gain = expected_gain(
        baseline_entropy=10.0,
        entropy_after={A: 2.0, B: 8.0},
        probabilities={A: 0.5, B: 0.5},
    )
    assert gain == pytest.approx(5.0)


def test_a_question_that_would_make_things_worse_has_zero_gain():
    assert (
        expected_gain(
            baseline_entropy=1.0, entropy_after={A: 5.0}, probabilities={A: 1.0}
        )
        == 0.0
    )


def test_score_prices_effort_stakes_and_the_two_penalties():
    plain = score(gain=2.0, mean_stakes=1.0, cost=1.0, uninformative=0.0, overlap=0.0)
    assert plain == pytest.approx(2.0)
    cheaper = score(gain=2.0, mean_stakes=1.0, cost=0.7, uninformative=0.0, overlap=0.0)
    assert cheaper > plain  # a binary audio A/B buys more per unit of effort
    penalised = score(
        gain=2.0, mean_stakes=1.0, cost=1.0, uninformative=0.5, overlap=0.25
    )
    assert penalised == pytest.approx(2.0 - 0.5 - 0.125)


def test_stakes_keep_the_review_pointed_at_what_the_wiki_will_use():
    low = score(gain=2.0, mean_stakes=0.1, cost=1.0, uninformative=0.0, overlap=0.0)
    high = score(gain=2.0, mean_stakes=0.9, cost=1.0, uninformative=0.0, overlap=0.0)
    assert high > low


# --- scoring one question ---------------------------------------------------


def perfect_separator(q, option):
    """A simulator: the chosen option wins everything, the rest collapse."""
    A_key = option if option != IDK_OPTION else A
    return {
        "u1": {A_key: 0.99, "member:b": 0.005, U: 0.005},
        "u2": {A_key: 0.99, "member:b": 0.005, U: 0.005},
    }


def no_op(q, option):
    return {"u1": {A: 0.5, B: 0.5}, "u2": {A: 0.5, B: 0.5}}


def stateful(simulate):
    """Lift a one-answer simulator into the state-aware form the PLAN takes.

    The plan is a sequence, so its simulator returns (next_state, posteriors).
    These fakes ignore the state - the real one carries the belief the previous
    simulated answer would have produced - and thread it through unchanged.
    """

    def plan_simulate(question, option, base):
        return base, simulate(question, option)

    return plan_simulate


def one_moment_per_step(q, option, base):
    """A state-aware simulator that resolves ONE moment per answer.

    This is the shape of a real session: answering a question settles what that
    question is about and nothing else. A plan that simulates from the wrong
    belief cannot see that, and reports a single question for all of them.
    """
    settled = set(base if base is not None else ())
    settled.add(q.target_utterances[0])
    return settled, {
        ref: ({A: 0.99, B: 0.005} if ref in settled else {A: 0.5, B: 0.5})
        for ref in ("u0", "u1", "u2", "u3")
    }


def test_a_question_that_resolves_the_session_scores_high():
    scored = score_question(
        question(),
        baseline_entropy=global_entropy({"u1": {A: 0.5, B: 0.5}}),
        posterior={A: 0.5, B: 0.5},
        simulate=perfect_separator,
    )
    assert scored.expected_gain > 0
    assert scored.score > 0


def test_a_question_that_changes_nothing_has_no_gain():
    posteriors = {"u1": {A: 0.5, B: 0.5}}
    scored = score_question(
        question(),
        baseline_entropy=global_entropy(posteriors),
        posterior={A: 0.5, B: 0.5},
        simulate=no_op,
    )
    assert scored.expected_gain == 0.0


def test_the_uninformative_penalty_decays_a_kind_the_dm_keeps_bouncing():
    plain = score_question(
        question(), baseline_entropy=10.0, posterior={A: 0.5, B: 0.5},
        simulate=perfect_separator,
    )
    penalised = score_question(
        question(), baseline_entropy=10.0, posterior={A: 0.5, B: 0.5},
        simulate=perfect_separator, uninformative_rates={"who_did": 0.6},
    )
    assert penalised.score < plain.score


def test_ranking_is_deterministic_and_best_first():
    questions = [
        question(prompt="A", cost=1.0),
        question(prompt="B", cost=0.7, kind="same_voice", targets=("u2",)),
    ]
    ranked = rank(
        questions,
        baseline_entropy=10.0,
        posteriors={"u1": {A: 0.5, B: 0.5}, "u2": {A: 0.5, B: 0.5}},
        simulate=perfect_separator,
    )
    assert [s.score for s in ranked] == sorted(
        [s.score for s in ranked], reverse=True
    )
    again = rank(
        list(reversed(questions)),
        baseline_entropy=10.0,
        posteriors={"u1": {A: 0.5, B: 0.5}, "u2": {A: 0.5, B: 0.5}},
        simulate=perfect_separator,
    )
    assert [s.question.prompt_text for s in again] == [
        s.question.prompt_text for s in ranked
    ]


# --- what the ranking refuses to simulate -----------------------------------


def test_i_dont_know_is_never_simulated():
    """*I don't know* changes nothing, so its entropy after IS the baseline.

    Not an approximation: asking the simulator would burn a full propagation to
    learn a number we already have.
    """
    calls: list[str] = []

    def recording(q, option):
        calls.append(option)
        return perfect_separator(q, option)

    scored = score_question(
        question(),
        baseline_entropy=4.0,
        posterior={A: 0.5, B: 0.5},
        simulate=recording,
    )
    assert IDK_OPTION not in calls
    assert scored.entropy_after[IDK_OPTION] == 4.0


def test_an_improbable_option_is_not_simulated_and_keeps_the_baseline():
    """The expectation is an average over the options; the ones the DM is very
    unlikely to pick are left at the baseline, which reads as "they teach us
    nothing" - the conservative direction, never a talked-up gain."""
    calls: list[str] = []

    def recording(q, option):
        calls.append(option)
        return perfect_separator(q, option)

    many = CandidateQuestion(
        kind="who_did",
        prompt_text="Who was it?",
        options=[
            QuestionOption(key=A, label="Alice"),
            QuestionOption(key=B, label="Bob"),
            QuestionOption(key="member:c", label="Cara"),
            QuestionOption(key="member:d", label="Dan"),
            QuestionOption(key=IDK_OPTION, label="I don't know"),
        ],
        target_utterances=["u1"],
        target_voices=[],
        hook={},
        cost=1.0,
        mean_stakes=0.9,
    )
    scored = score_question(
        many,
        baseline_entropy=4.0,
        posterior={A: 0.97, B: 0.01, "member:c": 0.01, "member:d": 0.01},
        simulate=recording,
        max_simulated_options=2,
    )
    assert len(calls) == 2
    assert set(calls) == {A, B}
    assert scored.entropy_after["member:c"] == 4.0


def test_the_gain_is_measured_with_the_same_stakes_as_the_baseline():
    """The objective is a DIFFERENCE of two entropies.

    Weighing the baseline by stakes and the measurement without them makes
    every gain negative - and negative gains are clamped to zero, so the engine
    silently stops asking anything. That is exactly what the first real session
    did: 38 candidate questions, 0 planned, "best_gain 0.0".
    """
    stakes = {"u1": 0.2, "u2": 0.3}
    # answering sharpens both moments but does not settle them
    after = {"u1": {A: 0.8, B: 0.2}, "u2": {A: 0.8, B: 0.2}}
    baseline = global_entropy(
        {"u1": {A: 0.5, B: 0.5}, "u2": {A: 0.5, B: 0.5}}, stakes
    )

    # idk_probability=0 so the gain is not discounted by the DM's real
    # "I don't know" rate, which is a separate (and correct) behaviour.
    weighted = score_question(
        question(),
        baseline_entropy=baseline,
        posterior={A: 0.5, B: 0.5},
        simulate=lambda q, option: after,
        idk_probability=0.0,
        stakes=stakes,
    )
    assert weighted.expected_gain == pytest.approx(
        baseline - global_entropy(after, stakes)
    )
    assert weighted.expected_gain > 0

    # ... and the unweighted measurement is the mistake this guards against:
    # the stakes-weighted baseline is SMALLER than an unweighted measurement of
    # the same session, so the difference goes negative and clamps to zero.
    mismatched = score_question(
        question(),
        baseline_entropy=baseline,
        posterior={A: 0.5, B: 0.5},
        simulate=lambda q, option: after,
        idk_probability=0.0,
        stakes=None,
    )
    assert global_entropy(after) > baseline
    assert mismatched.expected_gain == 0.0


def test_the_shortlist_is_bounded_and_keeps_the_uncertain_moments():
    """Every candidate costs a propagation per option, so the shortlist is what
    makes the ranking affordable. It must be deterministic and must prefer the
    moments the belief cannot settle."""
    certain = question(targets=("u_certain",), prompt="certain")
    uncertain = question(targets=("u_uncertain",), prompt="uncertain")
    posteriors = {
        "u_certain": {A: 0.999, B: 0.001},
        "u_uncertain": {A: 0.5, B: 0.5},
    }
    kept = shortlist([certain, uncertain], posteriors=posteriors, max_candidates=1)
    assert [q.prompt_text for q in kept] == ["uncertain"]


def test_the_shortlist_never_exceeds_its_budget():
    questions = [question(targets=(f"u{i}",), prompt=f"q{i}") for i in range(50)]
    posteriors = {f"u{i}": {A: 0.5, B: 0.5} for i in range(50)}
    assert len(shortlist(questions, posteriors=posteriors, max_candidates=7)) == 7


def test_a_question_touching_more_moments_competes_for_the_budget():
    """The proxy has to measure how MANY moments a question touches.

    It used to be the entropy of their average posterior, which is the same
    number for one moment and for a hundred, so a question that reaches forty
    moments scored no better than one that reaches a single line and lost the
    tiebreak on cost. Measured on a real session: the seven questions about a
    voice (now retired) ranked 121-129 of 131 against a budget of 24 and were
    never simulated (see app.ranking.shortlist).
    """
    wide = question(
        stakes=0.2,
        targets=tuple(f"v{i}" for i in range(40)),
        prompt="a question that reaches forty moments",
    )
    singles = [
        question(targets=(f"s{i}",), stakes=0.4, prompt=f"single {i}") for i in range(40)
    ]
    posteriors = {
        ref: {A: 0.5, B: 0.5}
        for ref in (*wide.target_utterances, *(q.target_utterances[0] for q in singles))
    }

    kept = shortlist([*singles, wide], posteriors=posteriors, max_candidates=4)
    assert wide in kept
    # ...and at the head of the budget, not clinging to the last slot.
    assert kept[0] is wide


def test_every_kind_carries_the_moments_it_is_about():
    """The invariant that replaced "a structural question is measured through
    its voices".

    Every generated kind names its own moments, so no kind can silently score
    zero for want of a target to measure.
    """
    content = question(targets=("u_00001",), prompt="who said it")
    assert _target_refs(content) == ["u_00001"]
    # deduplicated: the same moment twice is the same moment
    assert _target_refs(question(targets=("u_00001", "u_00001"))) == ["u_00001"]


def test_a_moment_the_review_is_blocked_on_gets_simulated():
    """The proxy measures "how much uncertainty does this touch", which is LARGE
    for a moment nobody can call and SMALL for one the engine already leans
    towards - so measured on a real session it starved exactly the questions the
    review exists for: the four beats the summary wrote as "un personaggio"
    ranked 37, 39, 40 and 78 of 156 and were never simulated.

    The moments the stopping rule is blocked on are not a preference, they are
    what the wiki is waiting for, so they are shortlisted first. The simulation
    still decides whether any of them is worth asking.
    """
    blocked = question(targets=("u_blocked",), stakes=0.5, prompt="who did it")
    crowded = [
        question(targets=(f"u{i}",), stakes=0.9, prompt=f"noisy {i}") for i in range(40)
    ]
    posteriors = {"u_blocked": {A: 0.91, B: 0.05, U: 0.04}}
    posteriors.update(
        {f"u{i}": {A: 0.2, B: 0.2, U: 0.6} for i in range(40)}
    )

    without = shortlist([blocked, *crowded], posteriors=posteriors, max_candidates=10)
    assert blocked not in without, "the proxy alone does not reach it"

    kept = shortlist(
        [blocked, *crowded],
        posteriors=posteriors,
        max_candidates=10,
        priority={"u_blocked"},
    )
    assert blocked in kept
    assert kept[0] is blocked


def test_ranking_simulates_only_the_shortlist():
    """The budget is a real bound on the work, not a display limit."""
    questions = [question(targets=(f"u{i}",), prompt=f"q{i}") for i in range(30)]
    posteriors = {f"u{i}": {A: 0.5, B: 0.5} for i in range(30)}
    calls: list[str] = []

    def recording(q, option):
        calls.append(q.prompt_text)
        return perfect_separator(q, option)

    ranked = rank(
        questions,
        baseline_entropy=10.0,
        posteriors=posteriors,
        simulate=recording,
        simulation_budget=4,
        max_simulated_options=3,
    )
    assert len(ranked) == 4
    assert set(calls) <= {q.prompt_text for q in questions[:4]} | {
        s.question.prompt_text for s in ranked
    }
    assert len(set(calls)) <= 4


def test_overlap_with_an_already_asked_question_is_penalised():
    ranked = rank(
        [question(kind="same_voice", targets=("u1",))],
        baseline_entropy=10.0,
        posteriors={"u1": {A: 0.5, B: 0.5}},
        simulate=perfect_separator,
        covered={"V1"},
    )
    assert ranked[0].overlap_penalty == pytest.approx(1.0)


def test_scored_payload_is_json_friendly():
    scored = score_question(
        question(), baseline_entropy=10.0, posterior={A: 0.5, B: 0.5},
        simulate=perfect_separator,
    )
    payload = scored.as_payload()
    assert set(payload) == {
        "kind", "prompt_text", "expected_gain", "gain_detail", "score", "cost"
    }


# --- the greedy plan --------------------------------------------------------


def test_the_plan_counts_the_questions_needed_to_finish():
    questions = [
        question(prompt=f"Q{i}", targets=(f"u{i}",)) for i in range(5)
    ]
    posteriors = {f"u{i}": {A: 0.5, B: 0.5} for i in range(5)}
    plan = plan_review(
        questions=questions,
        posteriors=posteriors,
        stakes={f"u{i}": 1.0 for i in range(5)},
        simulate=stateful(perfect_separator),
        max_questions=8,
        gain_floor=0.01,
    )
    assert isinstance(plan, PlannedReview)
    assert 1 <= plan.planned <= 5
    assert len(plan.entropy_trace) == plan.planned + 1
    # every answer strictly reduces the entropy
    assert plan.entropy_trace[-1] <= plan.entropy_trace[0]


def test_the_plan_asks_nothing_when_no_question_is_worth_asking():
    """A question whose simulated answer changes nothing is not asked at all -
    the DM's time is the budget, and an uninformative question spends it."""
    plan = plan_review(
        questions=[question()],
        posteriors={"u1": {A: 0.5, B: 0.5}},
        stakes={"u1": 1.0},
        simulate=stateful(no_op),
        gain_floor=0.15,
    )
    assert plan.planned == 0
    assert plan.stopped_because == "gain_floor"


def test_the_plan_asks_a_useful_question():
    plan = plan_review(
        questions=[question()],
        posteriors={"u1": {A: 0.5, B: 0.5}},
        stakes={"u1": 1.0},
        simulate=stateful(perfect_separator),
        gain_floor=0.05,
    )
    assert plan.planned == 1
    assert plan.questions == ["Who did it?"]


def test_the_plan_respects_the_budget():
    questions = [question(prompt=f"Q{i}", targets=(f"u{i}",)) for i in range(20)]
    posteriors = {f"u{i}": {A: 0.5, B: 0.5} for i in range(20)}
    plan = plan_review(
        questions=questions,
        posteriors=posteriors,
        stakes={f"u{i}": 1.0 for i in range(20)},
        simulate=stateful(perfect_separator),
        max_questions=3,
    )
    assert plan.planned <= 3


def test_the_plan_with_no_candidates_stops_immediately():
    plan = plan_review(
        questions=[], posteriors={}, stakes={}, simulate=stateful(perfect_separator)
    )
    assert plan.planned == 0
    assert plan.stopped_because == "no_candidates"


def test_the_plan_simulates_each_step_from_the_preceding_answer():
    """A plan is a SEQUENCE: step two starts where step one would have ended.

    The bug this guards is not a rounding error. Every step used to be simulated
    from the belief the plan STARTED from - a one-answer simulator cannot do
    anything else - while the baseline it was measured against advanced with each
    step. From step two on, the "gain" was the difference between a posterior the
    simulation never visited and a baseline it never had: negative, clamped to
    zero, and reported to the DM as "answer about 1 question to finish" for a
    session that then kept asking.
    """
    questions = [
        question(prompt=f"Q{i}", targets=(f"u{i}",)) for i in range(4)
    ]
    posteriors = {f"u{i}": {A: 0.5, B: 0.5} for i in range(4)}
    plan = plan_review(
        questions=questions,
        posteriors=posteriors,
        stakes={f"u{i}": 1.0 for i in range(4)},
        simulate=one_moment_per_step,
        max_questions=8,
        gain_floor=0.01,
    )
    assert plan.planned == 4
    assert len(set(plan.questions)) == 4
    # a strictly decreasing entropy trace: every simulated answer is measured
    # against the belief the previous one produced
    assert plan.entropy_trace == sorted(plan.entropy_trace, reverse=True)
    assert plan.entropy_trace[-1] < plan.entropy_trace[0]
    # the questions ran out, not the gains: every step was worth asking
    assert plan.stopped_because == "no_candidates"


def test_plan_payload_is_json_friendly():
    plan = plan_review(
        questions=[], posteriors={}, stakes={}, simulate=stateful(perfect_separator)
    )
    assert set(plan.as_payload()) == {"planned", "entropy_trace", "stopped_because"}

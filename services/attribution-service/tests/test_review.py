"""Review tests: the questions, the budget, and when to stop asking."""


from app.inference import SAME_VOICE_EPS, Edge
from app.propagate import Belief, Node, Propagator, VoiceState
from app.questions import CandidateQuestion, QuestionOption
from app.review import (
    STOP_REASONS,
    ReviewPolicy,
    ReviewSession,
    high_stakes_without_actor,
    report,
    should_stop,
    top_candidates,
)
from app.statuses import StatusThresholds

A, B, U = "member:a", "member:b", "unknown"


def node(ref, **kw):
    base = {"ref": ref, "stakes": 0.8, "seconds": 10.0}
    base.update(kw)
    return Node(**base)


def belief(potentials=None):
    potentials = potentials or {
        "u1": {A: 0.5, B: 0.4, U: 0.1},
        "u2": {A: 0.5, B: 0.4, U: 0.1},
    }
    nodes = {
        ref: node(ref, voice_id="V1", similarity={other: 0.95 for other in potentials if other != ref})
        for ref in potentials
    }
    return Belief(
        candidates=[A, B, U],
        nodes=nodes,
        potentials=potentials,
        edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")] if len(potentials) > 1 else [],
        voices={"V1": VoiceState(id="V1", refs=tuple(potentials), posterior={A: 0.5, B: 0.4, U: 0.1})},
        corroborating={ref: None for ref in potentials},
        voice_lr={ref: 0.0 for ref in potentials},
        thresholds=StatusThresholds(),
    )


def question(prompt="Who cast Fireball?", targets=("u1",), stakes=0.9):
    return CandidateQuestion(
        kind="who_did",
        prompt_text=prompt,
        options=[
            QuestionOption(key=A, label="Alice"),
            QuestionOption(key=B, label="Bob"),
            QuestionOption(key="idk", label="I don't know"),
        ],
        target_utterances=list(targets),
        target_voices=[],
        hook={"quote": "I cast fireball", "audio": {"start": 0.0, "end": 4.0}},
        cost=1.0,
        mean_stakes=stakes,
    )


# --- the stopping rule (S11.2) ---------------------------------------------


def test_stop_reasons_are_a_contract():
    assert "repeated_dont_know" in STOP_REASONS
    assert "gain_floor" in STOP_REASONS
    assert "target_reached" in STOP_REASONS


def test_a_question_worth_asking_keeps_the_review_open():
    decision = should_stop(
        policy=ReviewPolicy(), best_gain=0.5, unresolved=0.4, unattributed_events=[]
    )
    assert decision.stop is False
    assert decision.as_payload()["stop"] is False


def test_nothing_worth_asking_stops_the_review():
    decision = should_stop(
        policy=ReviewPolicy(), best_gain=0.01, unresolved=0.4, unattributed_events=["u1"]
    )
    assert decision.stop is True
    assert decision.reason == "gain_floor"


def test_a_good_question_is_not_enough_while_high_stakes_moments_are_open():
    """Coverage is a SUMMARY; the stopping rule is driven by what the wiki will
    CONSUME. A session is not done while the moments that will become pages are
    still unattributed, even if the aggregate looks good (S11.2)."""
    decision = should_stop(
        policy=ReviewPolicy(),
        best_gain=0.5,
        unresolved=0.02,  # below the target ...
        unattributed_events=["u1"],  # ... but a high-stakes moment is open
    )
    assert decision.stop is False
    assert decision.detail["unattributed_events"] == 1


def test_the_review_stops_when_the_target_is_reached():
    decision = should_stop(
        policy=ReviewPolicy(),
        best_gain=0.5,
        unresolved=0.02,
        unattributed_events=[],
    )
    assert decision.stop is True
    assert decision.reason == "target_reached"


def test_high_stakes_without_actor_finds_the_blocking_moments():
    base = belief()
    verdicts = Propagator(base).verdicts(base)
    found = high_stakes_without_actor(base, verdicts, threshold=0.7)
    assert set(found) <= {"u1", "u2"}
    # a session whose high-stakes moments are all settled blocks nothing
    assert high_stakes_without_actor(base, verdicts, threshold=0.99) == []


# --- the session ------------------------------------------------------------


def session(**kw):
    base = belief(kw.pop("potentials", None))
    questions = kw.pop("questions", [question(), question(prompt="Who opened the door?", targets=("u2",))])
    return ReviewSession(
        propagator=Propagator(base),
        questions=questions,
        policy=kw.pop("policy", ReviewPolicy(gain_floor_bits=0.0, target_unresolved=0.0)),
        **kw,
    )


def test_the_status_reports_buckets_and_coverage():
    status = session().status()
    assert "coverage" in status and "buckets" in status
    assert status["questions_asked"] == 0
    assert status["finished"] is False


def test_next_question_returns_a_question_and_no_stop():
    scored, decision = session().next_question()
    assert scored is not None
    assert decision.stop is False
    assert scored.question.options[-1].key == "idk"


def test_the_budget_stops_the_review():
    review = session(policy=ReviewPolicy(max_questions=0, gain_floor_bits=0.0))
    scored, decision = review.next_question()
    assert scored is None
    assert decision.reason == "budget"


def test_answering_records_what_it_resolved_and_moves_on():
    review = session()
    scored, _ = review.next_question()
    outcome = review.answer(scored, A)
    assert review.status()["questions_asked"] == 1
    assert scored.question.target_utterances[0] in review.answered_refs
    assert outcome.coverage_after >= outcome.coverage_before


def test_two_dont_knows_in_a_row_end_the_review():
    """Question fatigue is real, and the DM must not be trapped: two consecutive
    'I don't know' answers stop the review rather than drilling on (S8.3)."""
    review = session(policy=ReviewPolicy(gain_floor_bits=0.0, max_consecutive_idk=2))
    for _ in range(2):
        scored, _ = review.next_question()
        if scored is None:
            break
        review.answer(scored, "idk")
    scored, decision = review.next_question()
    assert scored is None
    assert decision.reason == "repeated_dont_know"


def test_a_real_answer_resets_the_dont_know_streak():
    review = session(policy=ReviewPolicy(gain_floor_bits=0.0, max_consecutive_idk=2))
    scored, _ = review.next_question()
    review.answer(scored, "idk")
    assert review.consecutive_idk == 1
    scored, _ = review.next_question()
    if scored is not None:
        review.answer(scored, A)
        assert review.consecutive_idk == 0


def test_finish_produces_the_outcome_the_event_carries():
    review = session()
    outcome = review.finish()
    assert review.finished is True
    payload = outcome.as_payload()
    assert set(payload) == {
        "questions_asked", "questions_planned", "coverage_before",
        "coverage_after", "unresolved_after", "stop_reason",
    }
    assert payload["stop_reason"] == "finished"


def test_the_plan_is_the_greedy_simulation_not_a_promise():
    plan = session().plan()
    assert plan.planned >= 0
    assert len(plan.entropy_trace) == plan.planned + 1
    assert plan.stopped_because in {
        "converged", "gain_floor", "budget", "no_candidates", "stopping_criterion"
    }


def test_an_asked_question_is_not_asked_again():
    review = session()
    scored, _ = review.next_question()
    review.answer(scored, A)
    remaining = [q.prompt_text for q in review.remaining()]
    assert scored.question.prompt_text not in remaining


def test_answering_the_same_question_twice_is_impossible():
    review = session()
    scored, _ = review.next_question()
    review.answer(scored, A)
    assert all(
        s.question.prompt_text != scored.question.prompt_text for s in review.ranked()
    )


# --- small helpers ----------------------------------------------------------


def test_report_counts_the_buckets():
    base = belief()
    verdicts = Propagator(base).verdicts(base)
    summary = report(verdicts)
    assert summary["total"] == len(verdicts)
    assert summary["confident"] <= summary["total"]


def test_top_candidates_reads_as_a_sentence():
    assert top_candidates({}) == "nothing attributed"
    assert "unresolved" in top_candidates({"unresolved": 41, "auto_high": 3})

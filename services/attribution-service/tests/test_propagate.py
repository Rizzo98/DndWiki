"""Propagation tests: one answer is a global update, and it is measured."""


import pytest

from app.inference import SAME_VOICE_EPS, Edge
from app.propagate import (
    Belief,
    Node,
    PropagationOutcome,
    Propagator,
    VoiceState,
    contradiction,
)
from app.questions import CandidateQuestion, QuestionOption
from app.statuses import StatusThresholds

A = "member:a"
B = "member:b"
U = "unknown"


def node(ref, **kw):
    base = {"ref": ref, "stakes": 0.8, "seconds": 5.0}
    base.update(kw)
    return Node(**base)


def belief(**overrides):
    """Four utterances, two of them in one voice identity."""
    nodes = {
        "u1": node("u1", voice_id="V1", alpha=1.0, similarity={"u2": 0.95, "u3": 0.1}),
        "u2": node("u2", voice_id="V1", alpha=1.0, similarity={"u1": 0.95, "u3": 0.1}),
        "u3": node("u3", voice_id="V2", alpha=1.0, similarity={"u1": 0.1, "u2": 0.1}),
        "u4": node("u4", voice_id="V2", alpha=1.0, similarity={}),
    }
    potentials = {
        "u1": {A: 0.4, B: 0.0, U: 0.0},
        "u2": {A: 0.2, B: 0.0, U: 0.0},
        "u3": {A: 0.0, B: 0.3, U: 0.0},
        "u4": {A: 0.0, B: 0.0, U: 0.0},
    }
    voices = {
        "V1": VoiceState(id="V1", refs=("u1", "u2"), posterior={A: 0.6, B: 0.3, U: 0.1}),
        "V2": VoiceState(id="V2", refs=("u3", "u4"), posterior={A: 0.2, B: 0.6, U: 0.2}),
    }
    kwargs = {
        "candidates": [A, B, U],
        "nodes": nodes,
        "potentials": potentials,
        "edges": [Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        "voices": voices,
        "corroborating": {"u1": None, "u2": None, "u3": None, "u4": None},
        "voice_lr": {"u1": 0.0, "u2": 0.0, "u3": 0.0, "u4": 0.0},
        "thresholds": StatusThresholds(),
    }
    kwargs.update(overrides)
    return Belief(**kwargs)


def content_question(targets=("u1",), kind="who_did"):
    return CandidateQuestion(
        kind=kind,
        prompt_text="Who cast Fireball?",
        options=[
            QuestionOption(key=A, label="Alice"),
            QuestionOption(key=B, label="Bob"),
            QuestionOption(key="idk", label="I don't know"),
        ],
        target_utterances=list(targets),
        target_voices=[],
        hook={"quote": "I cast fireball", "audio": {"start": 0.0, "end": 4.0}},
        cost=1.0,
        mean_stakes=0.9,
    )


def structural_question(kind="same_voice", voices=("V1",)):
    return CandidateQuestion(
        kind=kind,
        prompt_text="Same person?",
        options=[
            QuestionOption(key="same", label="Same person"),
            QuestionOption(key="different", label="Different people"),
            QuestionOption(key="idk", label="I don't know"),
        ],
        target_utterances=[],
        target_voices=list(voices),
        hook={"audio_a": {"start": 0.0, "end": 4.0}, "audio_b": {"start": 9.0, "end": 13.0}},
        cost=0.7,
        mean_stakes=0.7,
    )


# --- reading ----------------------------------------------------------------


def test_inference_includes_the_answers_already_given():
    base = belief(answers={"u1": A})
    potentials = Propagator(base).graph_for(base).log_potentials
    assert potentials["u1"][A] > base.potentials["u1"][A]


def test_verdicts_are_assigned_to_every_utterance():
    verdicts = Propagator(belief()).verdicts(belief())
    assert set(verdicts) == {"u1", "u2", "u3", "u4"}
    assert all(v.status in {"unresolved", "auto_low", "auto_high", "propagated", "user_confirmed"} for v in verdicts.values())


def test_only_the_moments_an_answer_moved_are_marked_inferred():
    """`propagated` is a property of a MOMENT, not of the run.

    It used to be "any answer exists anywhere", which re-labelled every
    untouched utterance of the session as inferred from the DM's answer. Since
    `propagated` is deliberately stricter than auto_high (S7.1), that made
    answering a question push the session's confidence DOWN: a real session went
    from 15.24% identified to 14.70% after eight correct answers, which is the
    "progress bar goes backwards" the DM saw.
    """
    propagator = Propagator(belief())
    before, after = propagator.apply(content_question(targets=("u1",)), A)
    outcome = propagator.measure(before, after)
    # u2 is in the same voice identity, so the answer MOVED it; the answered
    # moment itself is not "inferred", it is what the DM said.
    assert "u2" in outcome.moved
    assert "u1" not in outcome.moved
    assert "u1" not in after.propagated_refs
    # u3/u4 are a different voice identity, untouched by this answer
    assert "u3" not in after.propagated_refs
    assert "u4" not in after.propagated_refs
    verdicts = propagator.verdicts(after)
    assert verdicts["u1"].status == "user_confirmed"
    assert verdicts["u3"].status != "propagated"


def test_an_untouched_moment_keeps_its_observed_verdict():
    """A corroborated moment must not be demoted onto the stricter propagated
    bar just because the DM answered something else."""
    base = belief(
        corroborating={"u1": None, "u2": None, "u3": "self_name", "u4": None},
        voice_lr={"u1": 0.0, "u2": 0.0, "u3": 0.0, "u4": 0.0},
    )
    # u3 is peaked AND corroborated: auto_high on its own evidence.
    base.potentials["u3"] = {A: 0.0, B: 4.0, U: -4.0}
    propagator = Propagator(base)
    assert propagator.verdicts(base)["u3"].status == "auto_high"

    answered, after = propagator.apply(content_question(targets=("u1",)), A)
    propagator.measure(answered, after)
    verdicts = propagator.verdicts(after)
    assert verdicts["u1"].status == "user_confirmed"
    assert verdicts["u3"].status == "auto_high"  # untouched by the answer


# --- applying an answer -----------------------------------------------------


def test_i_dont_know_changes_nothing():
    """The engine must be able to accept 'I can't tell' without inventing a
    label: the answer is recorded and the belief is left alone (S8.3)."""
    propagator = Propagator(belief())
    before, after = propagator.apply(content_question(), "idk")
    assert after.answers == {}
    assert after.potentials == before.potentials


def test_an_answer_clamps_the_answered_utterance():
    propagator = Propagator(belief())
    _, after = propagator.apply(content_question(targets=("u1",)), B)
    assert after.answers["u1"] == B
    posteriors = propagator.infer(after).posteriors
    assert posteriors["u1"][B] > 0.9


def test_one_answer_re_scores_everything_that_sounds_like_it():
    """This is the 'one answer resolves many moments' property, and it is why
    answering a who_is_voice question is worth a minute of the DM's time."""
    propagator = Propagator(belief())
    before_posterior = propagator.infer(belief()).posteriors["u2"][A]
    _, after = propagator.apply(content_question(targets=("u1",)), A)
    after_posterior = propagator.infer(after).posteriors["u2"][A]
    assert after_posterior > before_posterior


def test_an_answer_about_a_dissimilar_utterance_does_not_reach_across():
    propagator = Propagator(belief())
    _, after = propagator.apply(content_question(targets=("u1",)), A)
    # u3 sounds nothing like u1 and is in another identity
    assert after.potentials["u3"].get(A, 0.0) == 0.0


def test_an_answer_teaches_a_capability():
    nodes = {
        "u1": node("u1", voice_id="V1", capability_reqs=("spell:fireball",)),
        "u2": node("u2", voice_id="V2"),
    }
    base = belief(nodes=nodes, potentials={"u1": {A: 0.1}, "u2": {A: 0.0, B: 0.0}})
    propagator = Propagator(base)
    _, after = propagator.apply(content_question(targets=("u1",)), A)
    assert after.learned[A] == {"spell:fireball"}


def test_a_learned_capability_biases_every_other_requirement():
    nodes = {
        "u1": node("u1", voice_id="V1", capability_reqs=("spell:fireball",)),
        "u2": node("u2", voice_id="V2", capability_reqs=("spell:fireball",)),
    }
    base = belief(
        nodes=nodes,
        potentials={"u1": {A: 0.0, B: 0.0}, "u2": {A: 0.0, B: 0.0}},
        learned={A: {"spell:fireball"}},
    )
    potentials = Propagator(base).graph_for(base).log_potentials
    assert potentials["u2"][A] > potentials["u2"][B]


def test_a_split_answer_destroys_the_pooled_belief():
    propagator = Propagator(belief())
    _, after = propagator.apply(structural_question("same_voice"), "different")
    assert "V1" in after.split_voices
    assert after.effective_voice_of("u1") is None


def test_a_merge_answer_unions_two_identities():
    propagator = Propagator(belief())
    _, after = propagator.apply(
        structural_question("different_voice", voices=("V1", "V2")), "same"
    )
    assert after.effective_voice_of("u3") == "V1"
    assert after.effective_voice_of("u1") == "V1"


def test_a_who_is_voice_answer_places_the_whole_identity():
    propagator = Propagator(belief())
    question = structural_question("who_is_voice", voices=("V1",))
    _, after = propagator.apply(question, B)
    assert after.voice_answers["V1"] == B
    posteriors = propagator.infer(after).posteriors
    assert posteriors["u1"][B] > posteriors["u1"][A]
    assert posteriors["u2"][B] > posteriors["u2"][A]


# --- simulation -------------------------------------------------------------


def test_simulate_does_not_mutate_the_belief():
    propagator = Propagator(belief())
    snapshot = {ref: dict(values) for ref, values in propagator.belief.potentials.items()}
    propagator.simulate(content_question(), A)
    assert propagator.belief.potentials == snapshot
    assert propagator.belief.answers == {}


def test_simulate_returns_a_posterior_for_every_utterance():
    posteriors = Propagator(belief()).simulate(content_question(), A)
    assert set(posteriors) == {"u1", "u2", "u3", "u4"}
    for posterior in posteriors.values():
        assert sum(posterior.values()) == pytest.approx(1.0)


def test_simulating_the_same_answer_twice_gives_the_same_result():
    propagator = Propagator(belief())
    first = propagator.simulate(content_question(), A)
    second = propagator.simulate(content_question(), A)
    assert first == second


# --- measuring --------------------------------------------------------------


def test_measure_counts_the_moments_the_answer_resolved():
    propagator = Propagator(belief())
    before, after = propagator.apply(content_question(targets=("u1",)), A)
    outcome = propagator.measure(before, after)
    assert isinstance(outcome, PropagationOutcome)
    assert outcome.coverage_after >= outcome.coverage_before
    assert outcome.resolved_seconds >= 0.0


def test_measure_reports_only_utterances_that_became_confident():
    """A belief that shuffles probability around without deciding anything has
    not helped anybody, and the count must not pretend otherwise."""
    propagator = Propagator(belief())
    before, after = propagator.apply(content_question(), "idk")
    outcome = propagator.measure(before, after)
    assert outcome.resolved == ()


def test_measure_payload_is_json_friendly():
    propagator = Propagator(belief())
    before, after = propagator.apply(content_question(targets=("u1",)), A)
    payload = propagator.measure(before, after).as_payload()
    assert set(payload) == {
        "resolved_utterances", "resolved_sec", "voice_ids", "learned",
        "coverage_before", "coverage_after", "contradiction",
    }


# --- contradictions ---------------------------------------------------------


def test_an_answer_against_overwhelming_voice_evidence_is_flagged():
    """The answer is honoured FOR THAT UTTERANCE - a DM can be right about a
    guest using someone else's headset - but the engine must record that the
    voice disagreed, or a bad print would silently poison the campaign (S10.7)."""
    assert contradiction(
        belief(), ref="u1", candidate=B, voice_cosines={A: 0.95, B: 0.30}
    ) is True
    assert contradiction(
        belief(), ref="u1", candidate=A, voice_cosines={A: 0.95, B: 0.30}
    ) is False
    assert contradiction(
        belief(), ref="u1", candidate=B, voice_cosines={A: 0.5, B: 0.45}
    ) is False

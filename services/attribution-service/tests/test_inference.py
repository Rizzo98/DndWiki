"""Inference tests: hand-computable graphs, exact posteriors, no model needed."""

import math

import pytest

from app.channels import ChannelOutput, Claim, UtteranceFeatures, self_name
from app.inference import (
    CONTINUITY_EPS,
    NEUTRAL_EPS,
    SAME_VOICE_EPS,
    Edge,
    Graph,
    best_candidate,
    candidate_keys,
    compose_potential,
    entropy,
    global_entropy,
    identity_coupling_eps,
    identity_posteriors,
    infer,
    margin,
    max_entropy,
    softmax,
    trust_alpha,
    unresolved_nodes,
)

A, B, U = "member:a", "member:b", "unknown"


def lp(**kw):
    return {k: float(v) for k, v in kw.items()}


def graph(potentials, edges=(), candidates=(A, B, U)):
    return Graph(
        nodes=list(potentials),
        candidates=list(candidates),
        log_potentials={node: values for node, values in potentials.items()},
        edges=list(edges),
    )


# --- coupling strength ------------------------------------------------------


def test_coupling_is_maximal_only_for_a_MEASURED_pure_identity():
    assert identity_coupling_eps(1.0) == pytest.approx(SAME_VOICE_EPS)
    assert identity_coupling_eps(0.5) == pytest.approx((SAME_VOICE_EPS + NEUTRAL_EPS) / 2)
    assert identity_coupling_eps(0.0) == pytest.approx(NEUTRAL_EPS)


def test_an_unmeasured_purity_is_not_a_verified_one():
    """None means "nobody measured this", not "we checked and it is one person".

    Reading it as 1.0 is what let a session of seven voices collapse onto the DM.
    """
    assert identity_coupling_eps(None) == NEUTRAL_EPS
    assert identity_coupling_eps(None) != SAME_VOICE_EPS


def test_a_neutral_edge_is_inert():
    """At eps = 0.5 the factor cannot change any message: whatever the
    neighbourhood looks like, the node's own evidence decides."""
    with_neutral = infer(
        graph(
            {
                "u1": lp(**{A: 3.0, B: 0.0, U: -2.0}),
                "u2": lp(**{A: 0.0, B: 1.0, U: -2.0}),
            },
            edges=[Edge.make("u1", "u2", eps=NEUTRAL_EPS, kind="same_voice")],
        )
    )
    without = infer(
        graph(
            {
                "u1": lp(**{A: 3.0, B: 0.0, U: -2.0}),
                "u2": lp(**{A: 0.0, B: 1.0, U: -2.0}),
            },
            edges=[],
        )
    )
    for node in without.posteriors:
        assert with_neutral.posteriors[node] == pytest.approx(without.posteriors[node])


def test_an_unmeasured_identity_does_not_hand_one_utterance_the_whole_cluster():
    """The regression, at the size and shape it actually happened.

    A real session gave one identity 108 observations, 102 of them with a flat
    potential (the identity-evidence pass never classified them) and 6 that
    merely LOOKED like the narrator. At the measured-and-pure coupling the six
    dragged all 108 onto the DM at p ~ 0.99, the statuses refused to promote any
    of it, and the review went silent with one question asked. The unmeasured
    identity must report those 102 as the unknowns they are.
    """
    nodes = {f"u{i}": lp(**{A: 0.0, B: 0.0, U: 0.0}) for i in range(102)}
    nodes.update({f"n{i}": lp(**{A: 2.2, B: -0.37, U: -0.37}) for i in range(6)})
    edges = [
        Edge.make(left, right, eps=identity_coupling_eps(None), kind="same_voice")
        for left, right in zip(list(nodes)[:-1], list(nodes)[1:])
    ]
    result = infer(graph(nodes, edges=edges, candidates=(A, B, U)))
    flat = [result.posteriors[f"u{i}"] for i in range(102)]
    assert max(max(p.values()) for p in flat) < 0.6  # nothing claims to know
    assert max(margin(p) for p in flat) < 0.5


# --- distributions ----------------------------------------------------------


def test_softmax_normalises():
    posterior = softmax({A: math.log(9.0), B: math.log(1.0)})
    assert posterior[A] == pytest.approx(0.9)
    assert posterior[B] == pytest.approx(0.1)


def test_softmax_of_an_impossible_distribution_is_uniform_not_nan():
    posterior = softmax({A: float("-inf"), B: float("-inf")})
    assert posterior == {A: 0.5, B: 0.5}


def test_softmax_of_nothing_is_empty():
    assert softmax({}) == {}


def test_entropy_and_margin():
    assert entropy({A: 1.0}) == 0.0
    assert entropy({A: 0.5, B: 0.5}) == pytest.approx(math.log(2))
    assert margin({A: 0.7, B: 0.2, U: 0.1}) == pytest.approx(0.5)
    assert margin({A: 1.0}) == 1.0


def test_best_candidate_breaks_ties_deterministically():
    key, value = best_candidate({"member:b": 0.5, "member:a": 0.5})
    assert key == "member:a" and value == 0.5
    assert best_candidate({}) == (None, 0.0)


def test_max_entropy_and_global_entropy_weight_by_stakes():
    assert max_entropy(1) == 0.0
    assert max_entropy(4) == pytest.approx(math.log(4))
    posteriors = {"u1": {A: 0.5, B: 0.5}, "u2": {A: 0.5, B: 0.5}}
    assert global_entropy(posteriors) == pytest.approx(2 * math.log(2))
    weighted = global_entropy(posteriors, stakes={"u1": 1.0, "u2": 0.0})
    assert weighted == pytest.approx(math.log(2))


# --- alpha ------------------------------------------------------------------


def test_trust_alpha_is_the_product_of_the_three_trust_factors():
    assert trust_alpha(purity=0.8, quality=0.5, cosine=1.0) == pytest.approx(0.4)


def test_trust_alpha_defaults_to_full_trust_when_nothing_is_known():
    assert trust_alpha(purity=None, quality=None, cosine=None) == 1.0


def test_trust_alpha_clamps_out_of_range_inputs():
    assert trust_alpha(purity=2.0, quality=-1.0, cosine=0.5) == 0.0
    assert trust_alpha(purity=1.0, quality=1.0, cosine=2.0) == 1.0


# --- potential --------------------------------------------------------------


def test_compose_potential_adds_prior_and_channels_in_log_space():
    output = self_name(
        UtteranceFeatures(
            ordinal=1,
            start=0.0,
            end=1.0,
            claims=(Claim("self_character", "Aramil", 1.0),),
        ),
        name_to_candidate={"aramil": A},
        candidates=[A, B],
    )
    potential = compose_potential(
        prior={A: 0.5, B: 0.5}, channel_outputs=[output]
    )
    assert potential[A] == pytest.approx(math.log(0.5) + 3.5)
    assert potential[B] == pytest.approx(math.log(0.5) - 0.5)


def test_compose_potential_applies_the_identity_exponent():
    """alpha_u interpolates between 'trust the cluster' (1) and 'ignore it' (0).

    p**alpha with alpha in [0, 1] is exactly that interpolation: alpha=1 leaves
    the identity's posterior as-is, alpha=0 flattens it to uniform, so the
    utterance is decided by its own channels alone.
    """
    glued = compose_potential(
        prior={A: 0.5, B: 0.5},
        channel_outputs=[],
        identity_posterior={A: 0.9, B: 0.1},
        alpha=1.0,
    )
    free = compose_potential(
        prior={A: 0.5, B: 0.5},
        channel_outputs=[],
        identity_posterior={A: 0.9, B: 0.1},
        alpha=0.0,
    )
    # the cluster's preference SURVIVES at alpha=1 and vanishes at alpha=0
    assert glued[A] - glued[B] == pytest.approx(math.log(9.0))
    assert free[A] - free[B] == pytest.approx(0.0)
    assert free[A] == pytest.approx(math.log(0.5))


def test_compose_potential_honours_per_channel_weights():
    output = ChannelOutput(channel="narration", log_lr={A: 2.0})
    plain = compose_potential(prior={A: 0.5, B: 0.5}, channel_outputs=[output])
    muted = compose_potential(
        prior={A: 0.5, B: 0.5}, channel_outputs=[output], weights={"narration": 0.0}
    )
    assert plain[A] == pytest.approx(math.log(0.5) + 2.0)
    assert muted[A] == pytest.approx(math.log(0.5))


def test_compose_potential_of_nothing_is_empty():
    assert compose_potential(prior={}, channel_outputs=[]) == {}


# --- level 2: identity posteriors -------------------------------------------


def test_identity_posteriors_are_a_softmax_over_the_pooled_evidence():
    pooled = {"V1": {A: math.log(3.0), B: 0.0}, "V2": {A: 0.0, B: 0.0}}
    posteriors = identity_posteriors(pooled)
    assert posteriors["V1"][A] == pytest.approx(0.75)
    assert posteriors["V2"][A] == pytest.approx(0.5)


def test_identity_posteriors_apply_the_campaign_prior():
    pooled = {"V1": {A: 0.0}}
    flat = identity_posteriors(pooled)
    skewed = identity_posteriors(pooled, prior={A: 0.9, B: 0.1})
    assert flat["V1"][A] == pytest.approx(1.0)
    assert skewed["V1"][B] == pytest.approx(0.1)


# --- belief propagation -----------------------------------------------------


def test_a_single_node_without_edges_is_just_its_potential():
    # 'unknown' is always a candidate, so it carries its own mass (e**0 = 1).
    result = infer(graph({"u1": lp(**{A: math.log(9.0), B: math.log(1.0), U: 0.0})}))
    assert result.posteriors["u1"][A] == pytest.approx(9.0 / 11.0, abs=1e-6)
    assert result.method == "bp"
    assert result.converged is True


def test_posteriors_always_sum_to_one():
    result = infer(
        graph(
            {"u1": lp(**{A: 1.0, B: 0.5, U: 0.0}), "u2": lp(**{A: 0.0, B: 1.0, U: 0.0})},
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    for posterior in result.posteriors.values():
        assert sum(posterior.values()) == pytest.approx(1.0)


def test_a_same_voice_edge_pulls_the_weaker_utterance_toward_the_stronger():
    """Two observations in one identity are usually one person: the confident
    one should win the unsure one over."""
    result = infer(
        graph(
            {
                "u1": lp(**{A: 3.0, B: 0.0, U: -2.0}),
                "u2": lp(**{A: 0.0, B: 1.0, U: -2.0}),
            },
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    assert result.posteriors["u2"][A] > result.posteriors["u2"][B]


def test_a_perfectly_symmetric_edge_stays_undecided():
    """A tie with no asymmetry is genuinely undecided: the engine must not
    invent a winner, it must report an unresolved utterance."""
    result = infer(
        graph(
            {
                "u1": lp(**{A: 0.2, B: 0.0, U: 0.0}),
                "u2": lp(**{A: 0.0, B: 0.2, U: 0.0}),
            },
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    u1, u2 = result.posteriors["u1"], result.posteriors["u2"]
    # mirror symmetry: the two nodes swap roles exactly
    assert u1[A] == pytest.approx(u2[B], abs=1e-6)
    assert u1[B] == pytest.approx(u2[A], abs=1e-6)
    # and within each node the two members are indistinguishable, so the
    # marginal must stay wide: no invented winner
    assert u1[A] == pytest.approx(u1[B], abs=0.02)
    assert margin(u1) < 0.05


def test_a_strong_defection_beats_the_cluster_edge():
    """alpha_u is what makes 'one cluster, several people' work: an utterance
    whose own evidence is decisive leaves its cluster."""
    result = infer(
        graph(
            {
                "u1": lp(**{A: 8.0, B: 0.0, U: -10.0}),
                "u2": lp(**{A: 0.0, B: 8.0, U: -10.0}),
            },
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    assert result.posteriors["u1"][A] > 0.95
    assert result.posteriors["u2"][B] > 0.95


def test_a_continuity_edge_is_much_softer_than_a_same_voice_edge():
    soft = infer(
        graph(
            {"u1": lp(**{A: 1.0, B: 0.0, U: 0.0}), "u2": lp(**{A: 0.0, B: 0.0, U: 0.0})},
            edges=[Edge.make("u1", "u2", eps=CONTINUITY_EPS, kind="continuity")],
        )
    )
    hard = infer(
        graph(
            {"u1": lp(**{A: 1.0, B: 0.0, U: 0.0}), "u2": lp(**{A: 0.0, B: 0.0, U: 0.0})},
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    assert soft.posteriors["u2"][A] < hard.posteriors["u2"][A]


def test_edges_pointing_at_unknown_nodes_are_ignored():
    result = infer(
        graph(
            {"u1": lp(**{A: 1.0, B: 0.0, U: 0.0})},
            edges=[Edge.make("u1", "ghost", eps=SAME_VOICE_EPS, kind="same_voice")],
        )
    )
    # softmax({A: 1, B: 0, unknown: 0}) = e / (e + 2)
    assert result.posteriors["u1"][A] == pytest.approx(0.5761168848, abs=1e-6)


def test_an_empty_graph_is_an_empty_answer_not_a_crash():
    result = infer(graph({}))
    assert result.posteriors == {}
    assert result.method == "bp"


def test_an_unreachable_tolerance_falls_back_to_mean_field():
    result = infer(
        graph(
            {
                "u1": lp(**{A: 1.0, B: 0.0, U: 0.0}),
                "u2": lp(**{A: 0.0, B: 1.0, U: 0.0}),
            },
            edges=[Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice")],
        ),
        tolerance=0.0,
    )
    assert result.converged is False
    assert result.method == "mean_field"
    for posterior in result.posteriors.values():
        assert sum(posterior.values()) == pytest.approx(1.0)


def test_inference_is_deterministic():
    """A recompute of the same evidence must reach the same answer: that is what
    makes an attribution revision reproducible (S16)."""
    build = lambda: graph(
        {
            "u1": lp(**{A: 0.7, B: 0.2, U: 0.0}),
            "u2": lp(**{A: 0.2, B: 0.7, U: 0.0}),
            "u3": lp(**{A: 0.0, B: 0.0, U: 0.0}),
        },
        edges=[
            Edge.make("u1", "u2", eps=SAME_VOICE_EPS, kind="same_voice"),
            Edge.make("u2", "u3", eps=CONTINUITY_EPS, kind="continuity"),
            Edge.make("u1", "u3", eps=CONTINUITY_EPS, kind="continuity"),
        ],
    )
    first = infer(build())
    second = infer(build())
    assert first.posteriors == second.posteriors


def test_result_payload_records_the_diagnostics():
    result = infer(graph({"u1": lp(**{A: 1.0, B: 0.0, U: 0.0})}))
    payload = result.as_payload()
    assert set(payload) == {"converged", "sweeps", "method", "max_delta"}


# --- helpers ----------------------------------------------------------------


def test_unresolved_nodes_are_the_entropic_ones_worst_first():
    posteriors = {
        "sure": {A: 0.97, B: 0.02, U: 0.01},
        "torn": {A: 0.5, B: 0.5},
        "mild": {A: 0.7, B: 0.2, U: 0.1},
    }
    unresolved = unresolved_nodes(posteriors, pmin=0.90, margin_min=0.50)
    # ordered by entropy: a 3-way split of 0.7/0.2/0.1 is LESS certain than a
    # clean 50/50, and the review should start with the worst moment
    assert unresolved == ["mild", "torn"]
    assert "sure" not in unresolved


def test_candidate_keys_always_include_unknown():
    assert candidate_keys([A, B]) == [A, B, U]
    assert candidate_keys([A, U]) == [A, U]
    assert candidate_keys([]) == [U]

"""The batched inference must agree with the straightforward one.

The per-edge/per-option version was 17 seconds for a single inference on a real
session, which is why the batched one exists. Batching is only allowed to change
the SPEED, so this file keeps a literal transcription of the original loops and
demands the two agree - on graphs big enough for the difference to show.
"""

import math
import random

import numpy as np
import pytest

from app.inference import (
    Edge,
    Graph,
    _beliefs,
    _edges,
    _mean_field,
    _pairwise_messages,
    infer,
    logsumexp,
)


def _naive_pairwise_message(base, log_same, log_diff):
    """The original, one edge and one option at a time."""
    shifted = base + log_diff
    out = np.empty_like(shifted)
    for j in range(shifted.size):
        if shifted.size == 1:
            others = float("-inf")
        else:
            others = logsumexp(np.delete(shifted, j))
        out[j] = np.logaddexp(others, base[j] + log_same)
    return out


def _naive_mean_field(graph, log_phi, *, iterations=50, damping=0.5):
    """The original per-node mean field, one node and one neighbour at a time."""
    k = len(graph.candidates)
    n = len(graph.nodes)
    index = {node: i for i, node in enumerate(graph.nodes)}
    neighbours = [[] for _ in range(n)]
    for edge in graph.edges:
        a, b = index[edge.left], index[edge.right]
        neighbours[a].append((b, edge.log_same, edge.log_diff))
        neighbours[b].append((a, edge.log_same, edge.log_diff))

    q = np.full((n, k), 1.0 / k)
    for _ in range(iterations):
        updated = np.empty_like(q)
        for i in range(n):
            field = log_phi[i].copy()
            for j, log_same, log_diff in neighbours[i]:
                same = q[j] * math.exp(log_same)
                diff = (1.0 - q[j]) * math.exp(log_diff)
                field += np.log(np.maximum(same + diff, 1e-12))
            shifted = field - field.max()
            weights = np.exp(shifted)
            total = weights.sum()
            updated[i] = weights / total if total > 0 else 1.0 / k
        q = damping * q + (1.0 - damping) * updated
    return q


def _naive_infer(graph, *, sweeps=10, max_sweeps=30, damping=0.5, tolerance=1e-4):
    """A transcription of the ORIGINAL loop structure, used as the oracle.

    Includes the mean-field fallback, because on a big graph loopy BP genuinely
    does not settle and the fallback is what produces the answer.
    """
    from app.inference import _directed_graph, _normalise, softmax

    n, k = len(graph.nodes), len(graph.candidates)
    candidate_index = {c: i for i, c in enumerate(graph.candidates)}
    log_phi = np.full((n, k), -50.0)
    for i, node in enumerate(graph.nodes):
        for candidate, value in graph.log_potentials.get(node, {}).items():
            j = candidate_index.get(candidate)
            if j is not None:
                log_phi[i, j] = float(value)

    directed = _directed_graph(graph)
    if directed is None:
        payload = {
            node: softmax(graph.log_potentials.get(node, {})) for node in graph.nodes
        }
        return payload, True, "bp"

    messages = np.zeros((directed.count, k))
    probabilities = np.full((n, k), 1.0 / k)
    previous = None
    converged = False
    for sweep in range(1, max_sweeps + 1):
        updated = messages.copy()
        for d in range(directed.count):
            u = directed.source[d]
            base = log_phi[u].copy()
            exclude = directed.reverse[d]
            for other in directed.incoming[u]:
                if other != exclude:
                    base += messages[other]
            updated[d] = (1.0 - damping) * _naive_pairwise_message(
                base, directed.log_same[d], directed.log_diff[d]
            ) + damping * messages[d]
        messages = updated
        beliefs = log_phi.copy()
        for d in range(directed.count):
            beliefs[directed.target[d]] += messages[d]
        probabilities = _normalise(beliefs)
        if previous is not None:
            max_delta = float(np.max(np.abs(probabilities - previous)))
            if sweep >= sweeps and max_delta <= tolerance:
                converged = True
                break
        previous = probabilities

    method = "bp"
    if not converged:
        # _naive_mean_field returns probabilities, so no _normalise() here -
        # see the note in app.inference.infer about what applying it did.
        probabilities = _naive_mean_field(graph, log_phi, damping=damping)
        method = "mean_field"
    payload = {
        node: {graph.candidates[j]: float(probabilities[i][j]) for j in range(k)}
        for i, node in enumerate(graph.nodes)
    }
    return payload, converged, method


def _random_graph(seed, n=60, k=6, edges=200):
    rng = random.Random(seed)
    nodes = [f"u{i:03d}" for i in range(n)]
    candidates = [f"member:{i}" for i in range(k - 1)] + ["unknown"]
    log_potentials = {
        node: {c: rng.uniform(-3.0, 1.0) for c in candidates} for node in nodes
    }
    graph_edges = []
    for _ in range(edges):
        left, right = rng.sample(nodes, 2)
        graph_edges.append(
            Edge.make(left, right, eps=rng.choice([0.05, 0.3, 0.49]), kind="same_voice")
        )
    return Graph(
        nodes=nodes, candidates=candidates, log_potentials=log_potentials, edges=graph_edges
    )


def test_the_batched_message_equals_the_per_option_message():
    rng = np.random.default_rng(4)
    base = rng.normal(size=(50, 6)) * 2.0
    log_same = np.full(50, math.log(0.95))
    log_diff = np.full(50, math.log(0.05))
    batched = _pairwise_messages(base, log_same, log_diff)
    for row in range(base.shape[0]):
        expected = _naive_pairwise_message(base[row], log_same[row], log_diff[row])
        assert np.allclose(batched[row], expected, atol=1e-12)


def test_the_batched_message_handles_a_single_candidate():
    base = np.array([[0.5], [-2.0]])
    batched = _pairwise_messages(
        base, np.array([math.log(0.95)] * 2), np.array([math.log(0.05)] * 2)
    )
    for row in range(2):
        expected = _naive_pairwise_message(
            base[row], math.log(0.95), math.log(0.05)
        )
        assert np.allclose(batched[row], expected, atol=1e-12)


def test_the_batched_beliefs_equal_the_edge_walk():
    graph = _random_graph(11)
    directed = __import__("app.inference", fromlist=["_directed_graph"])._directed_graph(graph)
    edges = _edges(directed)
    rng = np.random.default_rng(0)
    messages = rng.normal(size=(edges.count, len(graph.candidates)))
    log_phi = rng.normal(size=(len(graph.nodes), len(graph.candidates)))
    batched = _beliefs(log_phi, messages, edges)
    walked = log_phi.copy()
    for d in range(directed.count):
        walked[directed.target[d]] += messages[d]
    assert np.allclose(batched, walked, atol=1e-12)


def test_the_mean_field_fallback_is_not_flattened():
    """A potential that overwhelmingly favours one candidate must survive.

    _mean_field returns probabilities; running _normalise (which is for
    log-beliefs, and exponentiates) over them flattened a 0.9996 into a 0.28.
    Nothing downstream could then reach auto_high, so coverage stayed at zero
    and the review had nothing worth asking.
    """
    graph = _random_graph(5, n=12, k=4, edges=20)
    node = graph.nodes[0]
    graph.log_potentials[node] = {
        "member:0": 12.0,
        "member:1": -4.0,
        "member:2": -4.0,
        "unknown": -4.0,
    }
    # max_sweeps=1 forces the fallback deterministically, instead of hoping the
    # graph happens to oscillate
    result = infer(graph, max_sweeps=1)
    assert result.method == "mean_field"
    assert result.posteriors[node]["member:0"] > 0.99


@pytest.mark.parametrize("seed", [1, 3, 7, 12])
def test_the_batched_inference_reaches_the_same_posteriors(seed):
    """Same converged flag, same method, same numbers - on graphs both big
    enough to be interesting and dense enough that BP oscillates."""
    graph = _random_graph(seed)
    expected, expected_converged, expected_method = _naive_infer(graph)
    result = infer(graph)
    assert result.converged is expected_converged
    assert result.method == expected_method
    for node in graph.nodes:
        for candidate in graph.candidates:
            assert result.posteriors[node][candidate] == pytest.approx(
                expected[node][candidate], abs=1e-9
            )


def test_the_batched_mean_field_equals_the_neighbour_walk():
    """The fallback path is what actually runs on a real session (loopy BP does
    not settle on a 400-node graph), so it needs the same treatment."""
    graph = _random_graph(3)

    k = len(graph.candidates)
    candidate_index = {c: i for i, c in enumerate(graph.candidates)}
    log_phi = np.full((len(graph.nodes), k), -50.0)
    for i, node in enumerate(graph.nodes):
        for candidate, value in graph.log_potentials.get(node, {}).items():
            log_phi[i, candidate_index[candidate]] = float(value)

    index = {node: i for i, node in enumerate(graph.nodes)}
    neighbours = [[] for _ in graph.nodes]
    for edge in graph.edges:
        a, b = index[edge.left], index[edge.right]
        neighbours[a].append((b, edge.log_same, edge.log_diff))
        neighbours[b].append((a, edge.log_same, edge.log_diff))

    # the oracle: the original per-node loop
    q = np.full((len(graph.nodes), k), 1.0 / k)
    for _ in range(50):
        updated = np.empty_like(q)
        for i in range(len(graph.nodes)):
            field = log_phi[i].copy()
            for j, log_same, log_diff in neighbours[i]:
                same = q[j] * math.exp(log_same)
                diff = (1.0 - q[j]) * math.exp(log_diff)
                field += np.log(np.maximum(same + diff, 1e-12))
            shifted = field - field.max()
            weights = np.exp(shifted)
            total = weights.sum()
            updated[i] = weights / total if total > 0 else 1.0 / k
        q = 0.5 * q + 0.5 * updated

    batched = _mean_field(log_phi, graph, candidate_index, k)
    assert np.allclose(batched, q, atol=1e-9)

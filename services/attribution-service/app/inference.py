"""Fusion and inference: from channel log LRs to a posterior per utterance.

The model is a three-level hierarchy (docs/attribution-model.md S4.3):

    level 1  clustering   observation -> voice identity        (speaker-service)
    level 2  identity     voice identity -> member             (small)
    level 3  defection    utterance -> member, overriding its identity

The potential of one utterance is

    phi_u(x) = prior(x) * VI_posterior(x) ** alpha_u * PROD_e LR_e(u, x)

and `alpha_u = purity(VI(u)) * quality(u) * cosine(e_u, centroid(VI(u)))` is
what makes "one cluster, several people" work with no special case: an utterance
at the edge of an impure cluster, or one whose own embedding sits far from its
cluster's centroid, is free to defect from it. A clean utterance in a pure
cluster is glued to it.

The joint distribution adds two edge families:

    same_voice  (1-eps) if equal else eps   - observations that are one voice
    continuity  turn-taking prior           - consecutive turns alternate

Inference is damped loopy belief propagation over that graph, with a mean-field
fallback when it does not converge. Convergence is a diagnostic, never a
failure: the engine records it and carries on.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.channels import UNKNOWN, ChannelOutput

#: Default edge parameters. `same_voice` is strongly attractive - two
#: observations in one identity are usually one person - but not absolute, so an
#: utterance can still defect. `continuity` is much softer: at a table people
#: do answer themselves, and the DM narrates over the top of everyone.
SAME_VOICE_EPS = 0.05
CONTINUITY_EPS = 0.30
#: The eps at which the `same_voice` factor says NOTHING: "same person" and
#: "different people" are equally likely, so the edge leaves the messages
#: untouched and the channels decide the utterance alone.
NEUTRAL_EPS = 0.5


def identity_coupling_eps(
    purity: float | None, *, measured_eps: float = SAME_VOICE_EPS
) -> float:
    """How hard one identity's utterances are clamped to a single person.

    A `same_voice` edge is an ASSERTION - "these observations are one person" -
    and the strength of an assertion has to be the confidence we have in it,
    which is what `purity` measures (S5.1). A purity that was never measured is
    NOT purity 1.0: an unmeasured identity gets the neutral eps, where the factor
    is inert and the channels decide.

    This is not a nuance. At eps=0.05 a cluster is a near-hard constraint, so a
    handful of utterances that merely LOOK like the narrator (the narration
    channel is a +2.2 log LR) drag the other hundred onto the DM, and mean field
    then reports the result as certainty. That is how a session of seven
    distinct voices came out as "The Dungeon Master" 419 times out of 419.
    """
    if purity is None:
        return NEUTRAL_EPS
    p = max(0.0, min(1.0, float(purity)))
    return measured_eps + (NEUTRAL_EPS - measured_eps) * (1.0 - p)


@dataclass(frozen=True)
class Edge:
    """An undirected factor between two utterances."""

    left: str
    right: str
    #: log probability of the two sharing a candidate ...
    log_same: float
    #: ... and of them differing.
    log_diff: float
    kind: str = "same_voice"

    @classmethod
    def make(
        cls, left: str, right: str, *, eps: float, kind: str
    ) -> Edge:
        eps = max(1e-6, min(0.5, eps))
        return cls(
            left=left,
            right=right,
            log_same=math.log(1.0 - eps),
            log_diff=math.log(eps),
            kind=kind,
        )


@dataclass
class Graph:
    """The utterance graph handed to inference."""

    nodes: list[str]
    candidates: list[str]
    #: node -> {candidate: log phi}
    log_potentials: dict[str, dict[str, float]]
    edges: list[Edge] = field(default_factory=list)

    def candidate_matrix(self) -> np.ndarray:
        return np.array(self.candidates, dtype=object)


@dataclass
class InferenceResult:
    """Posteriors plus the diagnostics the design requires us to record."""

    posteriors: dict[str, dict[str, float]]
    converged: bool
    sweeps: int
    method: str
    max_delta: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "converged": self.converged,
            "sweeps": self.sweeps,
            "method": self.method,
            "max_delta": round(self.max_delta, 6),
        }


# --- softmax / entropy ------------------------------------------------------


def logsumexp(values: Sequence[float] | np.ndarray) -> float:
    """Numerically stable log-sum-exp."""
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return float("-inf")
    maximum = float(np.max(array))
    if not np.isfinite(maximum):
        return maximum
    return maximum + float(np.log(np.sum(np.exp(array - maximum))))


def softmax(log_scores: Mapping[str, float], *, temperature: float = 1.0) -> dict[str, float]:
    """Normalise log scores into a probability distribution.

    All-`-inf` (a candidate set nothing can satisfy) yields a uniform
    distribution rather than a NaN: an impossible utterance is still an
    utterance, and the engine must record *unresolved*, not crash.
    """
    if not log_scores:
        return {}
    finite = {k: float(v) for k, v in log_scores.items()}
    if temperature != 1.0:
        finite = {k: v / max(1e-6, temperature) for k, v in finite.items()}
    total = logsumexp(list(finite.values()))
    if not math.isfinite(total):
        uniform = 1.0 / len(finite)
        return {k: uniform for k in finite}
    return {k: float(math.exp(v - total)) for k, v in finite.items()}


def entropy(posterior: Mapping[str, float]) -> float:
    """Shannon entropy in nats (0 for a point mass)."""
    return -sum(p * math.log(p) for p in posterior.values() if p > 0)


def margin(posterior: Mapping[str, float]) -> float:
    """Difference between the best and the second-best candidate."""
    values = sorted(posterior.values(), reverse=True)
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(values[0] - values[1])


def best_candidate(posterior: Mapping[str, float]) -> tuple[str | None, float]:
    """The most likely candidate, with a deterministic tie-break.

    Ties are real (two members with identical evidence) and must not resolve by
    dictionary order, which would make a recompute disagree with the run that
    produced the stored attribution. The alphabetically-first key wins, so the
    same evidence always names the same candidate.
    """
    if not posterior:
        return None, 0.0
    key = min(posterior, key=lambda k: (-float(posterior[k]), k))
    return key, float(posterior[key])


def max_entropy(n_candidates: int) -> float:
    """Entropy of the uniform distribution over n candidates (bits aside: nats)."""
    return math.log(n_candidates) if n_candidates > 1 else 0.0


def global_entropy(
    posteriors: Mapping[str, Mapping[str, float]],
    stakes: Mapping[str, float] | None = None,
) -> float:
    """H(X_U | E) = SUM_u stakes(u) * H(x_u | E), the ranking objective (S9.1)."""
    stakes = stakes or {}
    return float(
        sum(
            float(stakes.get(node, 1.0)) * entropy(posterior)
            for node, posterior in posteriors.items()
        )
    )


# --- the potential ----------------------------------------------------------


def trust_alpha(
    *, purity: float | None, quality: float | None, cosine: float | None
) -> float:
    """How much an utterance trusts its voice identity (alpha_u, S4.3).

    Three ways to lose trust, all of them real: the cluster may hold several
    people (purity), the audio may be poor (quality), and this particular
    observation may sit far from what the cluster usually sounds like (cosine).
    Unknown values are treated as trustworthy: an unembedded utterance has no
    identity to defect from, and the channels decide it alone.
    """
    p = 1.0 if purity is None else max(0.0, min(1.0, purity))
    q = 1.0 if quality is None else max(0.0, min(1.0, quality))
    c = 1.0 if cosine is None else max(0.0, min(1.0, cosine))
    return float(p * q * c)


def compose_potential(
    *,
    prior: Mapping[str, float],
    channel_outputs: Sequence[ChannelOutput],
    identity_posterior: Mapping[str, float] | None = None,
    alpha: float = 1.0,
    weights: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """log phi_u(x) for every candidate.

    `prior` is the campaign prior over candidates (roster members plus
    `unknown`), and `identity_posterior` is level 2's belief about the
    utterance's voice identity, raised to `alpha`.
    """
    candidates = set(prior)
    for output in channel_outputs:
        candidates.update(output.log_lr)
    if identity_posterior:
        candidates.update(identity_posterior)
    if not candidates:
        return {}

    weights = weights or {}
    log_phi: dict[str, float] = {}
    for candidate in sorted(candidates):
        value = math.log(max(prior.get(candidate, 1e-6), 1e-9))
        if identity_posterior is not None:
            value += alpha * math.log(max(identity_posterior.get(candidate, 1e-9), 1e-9))
        for output in channel_outputs:
            weight = float(weights.get(output.channel, 1.0))
            value += weight * float(output.log_lr.get(candidate, 0.0))
        log_phi[candidate] = value
    return log_phi


def identity_posteriors(
    pooled: Mapping[str, Mapping[str, float]],
    *,
    prior: Mapping[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Level 2: an identity's belief about which member owns it.

    No exclusivity constraint is applied between identities, deliberately. The
    DM voices many NPCs and one player can be split across clusters, so forcing
    "each member owns at most one identity" would be wrong exactly where the
    system most needs to be right (S5.5). The structural constraints that DO
    hold - two people cannot speak at once - are enforced where they belong, in
    app.structure.
    """
    out: dict[str, dict[str, float]] = {}
    for handle, scores in pooled.items():
        combined = dict(scores)
        if prior:
            for candidate, p in prior.items():
                combined[candidate] = combined.get(candidate, 0.0) + math.log(
                    max(p, 1e-9)
                )
        out[handle] = softmax(combined)
    return out


# --- belief propagation -----------------------------------------------------


def _pairwise_messages(
    base: np.ndarray, log_same: np.ndarray, log_diff: np.ndarray
) -> np.ndarray:
    """Every edge's message at once: base is (E, k), the logs are (E,).

    m(x_v) = logsumexp_{x_u} [ base(x_u) + log psi(x_u, x_v) ], where the
    pairwise table has only two values - so each output entry is a logsumexp
    over the "same" branch plus a LEAVE-ONE-OUT logsumexp over "different".

    The leave-one-out is the subtle part. It is computed as
    log(S - exp(s_j)) with S = sum over all j, which is what makes the whole
    thing one expression instead of a loop - and it is the same quantity the
    per-option version computed, so the batched result is unchanged.

    Batching is not a micro-optimisation here. A real four-hour session is
    ~400 utterances and ~3600 edges, and the per-edge/per-option version spent
    17 SECONDS on a single inference - while the question ranking needs
    thousands of inferences to score its candidates (see app/ranking).
    """
    shifted = base + log_diff[:, None]
    row_max = shifted.max(axis=1, keepdims=True)
    weights = np.exp(shifted - row_max)
    total = weights.sum(axis=1, keepdims=True)
    leave_one_out = total - weights
    with np.errstate(divide="ignore"):
        others = np.where(
            leave_one_out > 0.0,
            row_max + np.log(np.maximum(leave_one_out, 1e-300)),
            -np.inf,
        )
    return np.logaddexp(others, base + log_same[:, None])


@dataclass
class _Directed:
    """The message graph: ONE MESSAGE PER DIRECTION of every edge.

    A single value per undirected edge is a tempting simplification and it is
    wrong: the message from u to v and the message from v to u are different
    quantities, and sharing one value makes a symmetric two-node graph come out
    asymmetric. The symmetry test in tests/test_inference.py is what caught it.
    """

    count: int
    source: list[int]
    target: list[int]
    reverse: list[int]
    log_same: list[float]
    log_diff: list[float]
    incoming: list[list[int]]


def _directed_graph(graph: Graph) -> _Directed | None:
    index = {node: i for i, node in enumerate(graph.nodes)}
    edges: list[tuple[int, int, float, float]] = []
    for edge in graph.edges:
        a, b = index.get(edge.left), index.get(edge.right)
        if a is None or b is None or a == b:
            continue
        edges.append((a, b, edge.log_same, edge.log_diff))
    if not edges:
        return None

    count = 2 * len(edges)
    source = [0] * count
    target = [0] * count
    reverse = [0] * count
    log_same = [0.0] * count
    log_diff = [0.0] * count
    incoming: list[list[int]] = [[] for _ in range(len(graph.nodes))]
    for e, (a, b, same, diff) in enumerate(edges):
        forward, backward = 2 * e, 2 * e + 1
        source[forward], target[forward] = a, b
        source[backward], target[backward] = b, a
        reverse[forward], reverse[backward] = backward, forward
        log_same[forward] = log_same[backward] = same
        log_diff[forward] = log_diff[backward] = diff
        incoming[b].append(forward)
        incoming[a].append(backward)
    return _Directed(count, source, target, reverse, log_same, log_diff, incoming)


@dataclass(frozen=True)
class _Edges:
    """The message graph as flat numpy arrays (one row per DIRECTED edge)."""

    count: int
    source: np.ndarray  # (E,) node index the message leaves
    target: np.ndarray  # (E,) node index the message arrives at
    reverse: np.ndarray  # (E,) index of the opposite direction of the same edge
    log_same: np.ndarray  # (E,)
    log_diff: np.ndarray  # (E,)


def _edges(directed: _Directed) -> _Edges:
    return _Edges(
        count=directed.count,
        source=np.asarray(directed.source, dtype=np.intp),
        target=np.asarray(directed.target, dtype=np.intp),
        reverse=np.asarray(directed.reverse, dtype=np.intp),
        log_same=np.asarray(directed.log_same, dtype=np.float64),
        log_diff=np.asarray(directed.log_diff, dtype=np.float64),
    )


def _scatter(rows: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    """Sum the rows of 'values' (E, k) into n buckets given by 'rows' (E,).

    np.add.at is the obvious spelling and it is roughly an order of magnitude
    slower than bincount on this size of array, which matters because it runs
    twice per sweep. The two agree on the accumulation ORDER (both walk the
    edges in index order), so the result is the same down to the last bit.
    """
    k = values.shape[1]
    flat = (rows[:, None] * k + np.arange(k)).ravel()
    summed = np.bincount(flat, weights=values.ravel(), minlength=n * k)
    return summed.reshape(n, k)


def _beliefs(log_phi: np.ndarray, messages: np.ndarray, edges: _Edges) -> np.ndarray:
    """b_u(x) = phi_u(x) * PROD over messages arriving at u."""
    return log_phi + _scatter(edges.target, messages, log_phi.shape[0])


def _message_updates(
    log_phi: np.ndarray, messages: np.ndarray, edges: _Edges
) -> np.ndarray:
    """Every directed message, computed from the PREVIOUS sweep in one pass.

    The message u->v leaves out the message coming back from v, which is the
    only place the graph's structure enters: summing the messages ARRIVING at u
    and subtracting the one that came from v is the same quantity as summing
    every incoming message except that one, and it is one array operation
    instead of a walk over the adjacency.
    """
    arriving = _scatter(edges.target, messages, log_phi.shape[0])
    base = log_phi[edges.source] + arriving[edges.source] - messages[edges.reverse]
    return _pairwise_messages(base, edges.log_same, edges.log_diff)


def infer(
    graph: Graph,
    *,
    sweeps: int = 10,
    max_sweeps: int = 30,
    damping: float = 0.5,
    tolerance: float = 1e-4,
) -> InferenceResult:
    """Damped loopy belief propagation with a mean-field fallback.

    Deterministic initialisation from the potentials alone, so a recompute of
    the same evidence reaches the same answer - which is what makes an
    attribution revision reproducible.

    Convergence is the change in the MARGINALS between sweeps: that is what a
    downstream consumer actually reads, and it is the quantity the design's "max
    change in any marginal" refers to (S4.4). Failing to converge is a
    diagnostic, not an error - the engine switches to mean field and records it.
    """
    n = len(graph.nodes)
    k = len(graph.candidates)
    if n == 0 or k == 0:
        return InferenceResult({}, True, 0, "bp", 0.0)

    candidate_index = {c: i for i, c in enumerate(graph.candidates)}
    log_phi = np.full((n, k), -50.0)
    for i, node in enumerate(graph.nodes):
        for candidate, value in graph.log_potentials.get(node, {}).items():
            j = candidate_index.get(candidate)
            if j is not None:
                log_phi[i, j] = float(value)

    directed = _directed_graph(graph)
    if directed is None:
        posteriors = {
            node: softmax(graph.log_potentials.get(node, {})) for node in graph.nodes
        }
        return InferenceResult(posteriors, True, 0, "bp", 0.0)

    edges = _edges(directed)
    messages = np.zeros((edges.count, k))
    probabilities = np.full((n, k), 1.0 / k)
    previous: np.ndarray | None = None
    converged = False
    used = 0
    max_delta = float("inf")

    for sweep in range(1, max_sweeps + 1):
        used = sweep
        # Synchronous update: every message is computed from the PREVIOUS
        # sweep's values, so the answer cannot depend on edge ordering.
        messages = (1.0 - damping) * _message_updates(
            log_phi, messages, edges
        ) + damping * messages

        probabilities = _normalise(_beliefs(log_phi, messages, edges))
        if previous is not None:
            max_delta = float(np.max(np.abs(probabilities - previous)))
        previous = probabilities
        if sweep >= sweeps and max_delta <= tolerance:
            converged = True
            break

    method = "bp"
    if not converged:
        # NOT _normalise()d again: _mean_field returns PROBABILITIES (rows that
        # already sum to 1), while _normalise expects log-beliefs and applies an
        # exp to them. Running it on probabilities is not a no-op - it flattens
        # them. A node whose potential overwhelmingly favours one candidate came
        # out at 0.28 instead of ~1.0, which meant nothing could ever clear the
        # auto_high bar, coverage stayed at 0.0 and no question was ever worth
        # asking. It only shows up HERE, on the fallback path, because that is
        # the path a real session takes: loopy BP does not settle on a
        # 400-utterance graph.
        probabilities = _mean_field(log_phi, graph, candidate_index, k)
        method = "mean_field"

    out: dict[str, dict[str, float]] = {}
    for i, node in enumerate(graph.nodes):
        out[node] = {graph.candidates[j]: float(probabilities[i][j]) for j in range(k)}
    return InferenceResult(out, converged, used, method, max_delta)


def _normalise(beliefs: np.ndarray) -> np.ndarray:
    """Row-normalise log beliefs into probabilities (rows of -inf -> uniform)."""
    shifted = beliefs - beliefs.max(axis=1, keepdims=True)
    weights = np.exp(shifted)
    totals = weights.sum(axis=1, keepdims=True)
    k = beliefs.shape[1]
    safe = np.where(totals > 0, totals, 1.0)
    normalised = weights / safe
    degenerate = (totals <= 0).ravel()
    if degenerate.any():
        normalised[degenerate] = 1.0 / k
    return normalised


def _mean_field(
    log_phi: np.ndarray,
    graph: Graph,
    candidate_index: Mapping[str, int],
    k: int,
    *,
    iterations: int = 50,
    damping: float = 0.5,
) -> np.ndarray:
    """Variational mean-field fallback when loopy BP does not settle.

    Each node's distribution is updated from its own potential and the CURRENT
    marginals of its neighbours. It converges to a stationary point of the
    Bethe-free approximation, which is worse than exact BP but far better than
    an unconverged message soup - and it always terminates.
    """
    n = len(graph.nodes)
    direction = _directed_graph(graph)
    if direction is None:
        return np.full((n, k), 1.0 / k)
    edges = _edges(direction)

    q = np.full((n, k), 1.0 / k)
    same_weight = np.exp(edges.log_same)[:, None]
    diff_weight = np.exp(edges.log_diff)[:, None]

    for _ in range(iterations):
        neighbour = q[edges.target]
        pair = neighbour * same_weight + (1.0 - neighbour) * diff_weight
        field = log_phi + _scatter(
            edges.source, np.log(np.maximum(pair, 1e-12)), n
        )
        shifted = field - field.max(axis=1, keepdims=True)
        weights = np.exp(shifted)
        totals = weights.sum(axis=1, keepdims=True)
        safe = np.where(totals > 0, totals, 1.0)
        updated = np.where(totals > 0, weights / safe, 1.0 / k)
        q = damping * q + (1.0 - damping) * updated
    return q


# --- helpers used by the question generator ---------------------------------


def unresolved_nodes(
    posteriors: Mapping[str, Mapping[str, float]], *, pmin: float, margin_min: float
) -> list[str]:
    """Utterances the belief cannot settle, worst first."""
    scored: list[tuple[float, str]] = []
    for node, posterior in posteriors.items():
        _, top = best_candidate(posterior)
        if top >= pmin and margin(posterior) >= margin_min:
            continue
        scored.append((entropy(posterior), node))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [node for _, node in scored]


def candidate_keys(candidates: Sequence[str]) -> list[str]:
    """Roster candidates plus the always-present `unknown`."""
    keys = list(dict.fromkeys(candidates))
    if UNKNOWN not in keys:
        keys.append(UNKNOWN)
    return keys

# ADR-0002: Evidence-first, question-driven speaker attribution

- **Status:** Accepted — implemented
- **Date:** 2026-02 (accepted and implemented the same cycle)
- **Implementation:** `services/attribution-service` (new), the per-observation
  evidence path in `services/speaker-service`, the text-only mode in
  `services/refiner-service`, the attribution statuses in
  `services/session-service`, the attribution gate in
  `services/content-service`, and the review UI in `apps/web`. See
  "Implementation status" in `docs/attribution-plan.md` for what is verified,
  what is measured, and what remains behind the flag.
- **Deciders:** Platform team
- **Supersedes (in part):** the `speaker_assignments` model described in
  `docs/data-model.md` and the identification stage of `docs/architecture.md` §6.

## Context

The platform currently models speaker attribution as a **1:1 mapping from a
diarization label to a campaign member**:

```
speaker_assignments(session_id, speaker_label UNIQUE, member_id, user_id,
                    confidence, status = pending | auto | confirmed)
```

Everything downstream keys off that mapping: `speaker-service` auto-assigns a
label when its pooled ECAPA-TDNN centroid scores `>= SPEAKER_MATCH_THRESHOLD`
(0.75), the session parks on `speaker_pending` until **every** label is
`confirmed`, `content-service` resolves `SPEAKER_XX -> character_name`
(`workers/generate.py::resolve_speaker_names`) and renders
`[HH:MM:SS] NAME: text` lines into the extraction prompt.

In production this model breaks in four independent ways:

1. **A cluster is not a person.** `SPEAKER_00` routinely contains segments from
   two people (adjacent speakers merged by the diarizer or by the UPGMA
   re-clustering in `speaker-service/app/relabel.py`), and one person routinely
   spans several clusters (microphone distance, shouting vs. whispering,
   agglomerative threshold too tight). The 1:1 key cannot represent either case,
   so `SPEAKER_00 -> 60% Player 1, 40% Player 2` is not storable and
   `SPEAKER_00 -> Player 1` silently applies to Player 2's turns.
2. **The DM is many voices.** The DM narrates, adjudicates rules, and voices
   every NPC. A single DM cluster is therefore guaranteed to be multi-modal, and
   the "one voice = one member" assumption is wrong for exactly the person who
   speaks the most.
3. **The decision is made before the evidence exists.** Voice similarity alone
   decides at 0.75 cosine. Character capabilities ("only the wizard can cast
   Fireball"), self-identification ("Aramil casts Shield"), addressivity
   ("Thorin, what do you do?") and turn-taking structure are all available in the
   transcript and are all ignored.
4. **The review asks the wrong question.** The DM is asked to confirm
   `SPEAKER_03 -> Player 2` while looking at an anonymous label, with no audio
   playback and no content hook. This is expensive for the DM, teaches the
   system almost nothing (one label ≈ one decision), and the whole pipeline
   **blocks** on it (`speaker_pending`).

The user-visible symptom is that the raw transcript is not useful for a
several-hour session, so the DM stops using it — and the wiki inherits whatever
mis-attribution the pipeline guessed.

## Decision

Replace label-to-member mapping with a **five-layer identity model and a
posterior over utterances**, and replace the confirm-the-label UI with an
**active-learning review** that asks the DM the minimum number of
highest-value questions.

1. **Five layers, not one.** `audio segment -> diarization cluster ->
   voice identity -> player (campaign member) -> character`. A diarization
   label is *never* an identity; it is one hypothesis about which audio belongs
   together. Voice identities are **anonymous and session-scoped**
   (`V1, V2, ...`) and may be split, merged or re-scoped at any time.

2. **Attribution is per utterance, stored as a distribution.** The unit of
   attribution is an *utterance* (a speaker turn, refined). Every utterance
   carries `posterior: {candidate -> probability}` over
   `{member:<id> ...} ∪ {unknown}`, plus a status
   (`auto_high | auto_low | user_confirmed | propagated | unresolved`) and the
   evidence that produced it. Cluster-level attribution is a **derived
   aggregate**, not the source of truth.

3. **Evidence fusion, not a single threshold.** Voice similarity, temporal
   continuity, addressee/adjacency, DnD capability constraints, self-naming,
   out-of-world markers and user answers are combined as calibrated
   log-likelihood ratios. Voice is one channel among several — currently it is
   the only one.

4. **A new `attribution-service` owns the inference and the review.**
   `speaker-service` is demoted to a **voice-evidence provider** (embeddings,
   similarity, multi-centroid per-member voice models, per-observation quality).
   It no longer decides and no longer writes `status: auto`.

5. **The DM reviews decisions, not labels.** Questions are generated from
   *events in the session* ("Who cast Fireball on the three goblins?") or as
   audio A/B comparisons ("Are these two the same person?"), never from
   `SPEAKER_XX`. Options are roster members rendered as
   `Player — Character`, plus *The Dungeon Master*, *Someone else*, and
   *I don't know*.

6. **Questions are chosen by expected global entropy reduction.** For each
   candidate question we simulate the propagation of every possible answer and
   measure the resulting entropy over *all* unresolved utterances, not just the
   queried one. The question with the highest expected gain per unit of DM
   effort is asked. The review stops when no remaining question clears a gain
   floor.

7. **Answers propagate globally.** An answer updates the member's voice model
   (multi-centroid), re-scores every utterance's voice channel, re-runs belief
   propagation over the utterance graph, may force a cluster split or merge, and
   may learn a character capability ("Player 3 can cast Fireball") that resolves
   every other utterance requiring it.

8. **The review does not block the pipeline.** `speaker_pending` is replaced by
   `attribution_review`, a resting state the DM can enter, skip or waive.
   Content generation runs from the *attributed* transcript, rendering
   unresolved utterances as unattributed rather than guessing.

9. **Only confident attribution reaches the wiki.** Character-page facts require
   `user_confirmed | auto_high | propagated-from-confirmed`. Unresolved
   utterances are rendered at party level ("the party opened the door") or
   omitted; a `SPEAKER_00` string can never reach a page, an event participant
   list or a timeline entry.

## Consequences

**Positive**

- One DM answer about an *event* resolves hundreds of utterances (the voice
  model update is global), instead of one DM answer about one label.
- Split/merge pathologies become representable *and* detectable: cluster purity
  is modelled, and "are these two the same person?" is the cheapest, highest-gain
  question type.
- The DM never sees the raw transcript as a work item; it is demoted to a
  collapsed, explicitly-untrusted artifact.
- Uncertainty becomes a first-class, queryable value, so the summary, the wiki
  planner and the UI can all behave differently for confident vs. unresolved
  facts.
- `speaker-service` gets simpler (no policy, no thresholds to tune): it becomes
  a measurable, replaceable embedding provider.

**Negative / trade-offs**

- One more service and one more database (`dnd_attribution`); mitigated because
  it replaces logic currently spread across `speaker-service`, `refiner-service`
  (LLM speaker guessing) and the web page.
- One additional LLM pass per session (identity-evidence extraction). Bounded:
  its output is a small JSON per chunk, it runs on the cheap model, and it
  replaces the refiner's current per-turn relabeling pass.
- The engine must be **calibrated** to be trustworthy; uncalibrated posteriors
  would be worse than the current cosine threshold. Mitigated by fitting
  per-campaign calibration on confirmed history and by a hand-labelled
  benchmark (see `docs/attribution-plan.md`).
- Existing `speaker_assignments` rows, `speakers.identified` /
  `speakers.assigned` consumers and the web speaker panel must be migrated
  behind a compatibility shim for one release.

## Alternatives considered

- **Keep label-to-member, add confidence + manual splits.** Rejected: it keeps
  the 1:1 key that is the root cause, and every downstream consumer keeps
  reading a lie.
- **Make the LLM relabel speakers from the transcript (extend the current
  refiner pass).** Rejected as the primary mechanism: the LLM has no access to
  the audio, cannot split a merged cluster, cannot hear two voices as the same
  person, and produces a *guess* indistinguishable from a *fact*. The LLM's
  legitimate roles are text correction and identity-evidence extraction.
- **Pure speaker-embedding improvement (better clustering, more thresholds).**
  Rejected as the primary mechanism: it optimizes one evidence channel and does
  nothing about the DM's multi-voice reality, capability evidence, or the cost
  of the review.
- **Ask the DM to label every cluster and skip the engine.** Rejected: it is the
  current UX and it is the thing being replaced.
- **Bayesian network over all utterances with exact inference.** Rejected as
  unnecessary: the hierarchical factorisation (utterance -> voice identity ->
  member, with utterance-level defections) makes loopy belief propagation over a
  few thousand nodes converge in milliseconds, which is what makes question
  ranking by simulated propagation affordable.

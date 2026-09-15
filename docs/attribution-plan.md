# Speaker attribution redesign — implementation plan

Companion to `docs/adr/0002-evidence-first-speaker-attribution.md`,
`docs/attribution-model.md` and `docs/attribution-ux.md`.

---

## 1. Delivery constraints

1. **The old path stays live until the new one is measured.** Everything is
   behind `ATTRIBUTION_ENABLED`. With the flag off, the pipeline behaves exactly
   as it does today, including `speaker_pending` blocking and the speaker panel.
2. **No big-bang migration.** `speaker_assignments` survives as a derived
   compatibility view for one release; the old endpoints read it.
3. **Every phase is independently shippable and independently revertible.**
4. **No claim without a measurement.** Phase 1 exists specifically to produce
   the labelled benchmark the later phases are judged against.

---

## 2. Phase 0 — foundations (no user-visible change)

**Goal:** the data the engine needs exists, and nothing behaves differently.

### speaker-service

- [ ] Emit **per-utterance observations** instead of (only) per-label pooled
      windows: embed each turn from `dnd_common.transcript.speaker_turns`,
      write to a new Qdrant collection `voice_observations` with
      `{campaign_id, session_id, utterance_id, voice_id, start_sec, end_sec,
      quality, model_version}`.
      Touches `app/workers/identify.py` (`_relabel`, `_match_raw`),
      `app/audio.py`, `app/qdrant.py`.
- [ ] Add **observation quality**: length, an energy/SNR estimate and a simple
      overlap heuristic. New module `app/quality.py`, pure and unit-testable
      (same style as `app/relabel.py`).
- [ ] Emit **multi-centroid member voice models** to `member_voice_models`
      instead of pooling into one anchor; return per-centroid similarities from
      the search API. Touches `_load_anchors`, `_match_relabeled`, `_match_raw`.
- [ ] **Fix the enrollment defects**: enroll per observation with a quality and
      purity gate instead of the pooled label window
      (`process_assigned`), and stop emitting `auto` from the anchor path
      without a threshold check (`_match_relabeled`).
- [ ] Publish **evidence** in `speakers.identified` (`nearest_centroids` with
      cosines and quality) alongside the legacy verdicts, so old consumers keep
      working during the transition.
- [ ] Move `upgma_cluster` and the turn-grouping into a shared, dependency-free
      module reachable by the new engine (candidate home: a new
      `attribution-service/app/clustering.py`, or `dnd_common` if both services
      keep needing it).

### refiner-service

- [ ] Add `REFINER_SPEAKERS=false` and, under it, drop the speaker-label part of
      the prompt: text corrections only, labels untouched. This is a prerequisite
      for the engine, which must see the *diarizer’s* labels, not the LLM’s
      guesses. Touches `app/prompts.py`, `app/refine.py`.
- [ ] **Fix the prompt-version drift first.** `refiner-service/app/prompts.py`
      declares `PROMPT_VERSION = "v3"` but `docker-compose.yml` sets
      `REFINER_PROMPT_VERSION` defaulting to `v2`, so the version recorded on the
      `transcription.refined` payload and in the refiner meta does not identify the
      prompt that actually ran. Every version-pinned claim in this design (and the
      existing `generation_jobs.prompt_version` in content-service) depends on that
      string being true; auditability is worthless if it drifts.
- [ ] **Delete the dead `enrolled_voiceprint` control.** `speakers.assigned`
      publishes it (and `confirm_assignment()` defaults it to `True` while the web
      client always sends `false`), but nothing in
      `services/speaker-service/app` reads it — enrollment happens unconditionally.
      The redesign replaces the flag with the quality and purity gate of §12.3,
      which is a property of the evidence rather than a caller-supplied boolean.

### session-service

- [ ] Add the new statuses to `app/status.py` (`attributing`,
      `attribution_ready`, `attribution_review`) and accept `speaker_pending` as
      an alias when `ATTRIBUTION_ENABLED` is on.

### attribution-service (new)

- [ ] Service skeleton following the existing pattern (`app/main.py`,
      `app/core/config.py`, `app/deps.py`, `app/broker.py`, `app/storage.py`,
      `app/clients/session_service.py`), DB `dnd_attribution`, Alembic
      `0001_initial.py` with the DDL of `docs/attribution-model.md` §3.2.
- [ ] **Utterance builder**: diarization segments to utterances, using
      `dnd_common.transcript.speaker_turns` plus word timings for sub-splitting.
      Pure function, heavily unit-tested. Two pitfalls to handle explicitly:
      **`speaker_turns` compares `cur.chunk == chunk` with `==`, so when segments
      carry no `chunk` key both sides are `None` and the chunk boundary silently
      disappears** — same-label runs then merge across the whole file; and every
      utterance must record the **segment indices** it came from
      (`utterances.segment_indices`), because the current
      `_rewrite_artifacts` maps labels back onto transcript segments by exact
      `(start, end)` tuple equality, which silently drops the label whenever
      timings collide or drift.
- [ ] **Voice identity clustering** at the observation level, with the
      campaign-calibrated cut; `purity` computed by the stability test of §5.1
      (also pure and unit-tested).

**Exit criteria:** with the flag off, byte-identical behaviour; with the flag on,
`dnd_attribution` is populated for a real session and the purity numbers on a
hand-checked session are sane.

---

## 3. Phase 1 — evidence, fusion, and the benchmark (still invisible to the DM)

**Goal:** the engine produces posteriors; the DM still sees the old panel. The
two are compared, and the comparison is the calibration data.

### attribution-service

- [ ] **Identity-evidence pass** (`app/evidence_prompt.py`,
      `app/evidence.py`): the schema and view of §6, chunked at 8k tokens, with
      the roster and known capabilities injected. `PROMPT_VERSION`-style
      versioning identical to content-service.
- [ ] **Capability store and solver** (`app/capabilities.py`): read the wiki
      character page `attributes.character.class` and
      `campaign_members.character_description`, mine the latter once per campaign
      into `member_capabilities`, and expose `compat(member, requirement)`.
- [ ] **Scoring channels** (`app/channels.py`): one pure function per channel of
      §4.2 returning `{candidate_key: log_lr}`. Each independently unit-tested
      with hand-written fixtures — this module is the intellectual core and must
      be testable without a database, a model or a queue.
- [ ] **Belief propagation** (`app/inference.py`): the hierarchical model of
      §4.3/§4.4 with damped loopy BP, NumPy, convergence diagnostics, and a
      deterministic fallback. Unit tests assert exact posteriors on small
      hand-computable graphs.
- [ ] **Split/merge detector** (`app/structure.py`): §5.1–§5.4, including the
      bootstrap stability test.
- [ ] **Local re-diarization** (`app/local_diarize.py`): the span re-processing of
      §5.3, calling speaker-service for embeddings.
- [ ] **Calibration** (`app/calibration.py`): KDE score→LLR, channel-weight
      logistic regression, per-campaign persistence, cold-start defaults.
      Calibration reads the campaign’s confirmed labels through
      `GET /internal/campaigns/{id}/speaker-history`, which caps at
      `limit_sessions` (query default 5, `ge=1 le=50`) and returns only
      **confirmed, user-linked** assignments. Both restrictions must be relaxed
      for calibration: it wants *all* labelled history of the campaign, not the
      five newest sessions, and it can use `member_id`-keyed labels that carry no
      user at all. Add an uncapped internal read rather than widening the
      existing one, which the enrollment path still depends on.
- [ ] Everything writes the tables of §3.2 but **no events are published yet**.

### The benchmark (this is a deliverable, not a chore)

- [ ] Hand-label **two real sessions** at utterance level: for each utterance, the
      true speaker (member or “outside the party” or “several/overlap”). One
      4-hour session is ~1500–2500 utterances; two sessions is a day of careful
      work and it is the only way any of the claims in this design can be
      checked. Store as JSON next to the transcripts (bucket
      `transcripts/{session_id}/ground_truth.json`).
- [ ] Build an **offline replay harness** (`tests/benchmark/`): run the engine over
      the labelled sessions with a scripted DM, and report turn accuracy,
      `auto_high` precision (the false-confident rate), split/merge accuracy and
      — the headline number — **questions asked to reach 90% coverage**.
- [ ] A **fake DM** policy for the harness: answers correctly with probability
      `p_correct`, answers *I don’t know* with probability `p_dontknow`; both are
      swept to see how the review degrades under a tired DM.

### shadow comparison

- [ ] For every session with `ATTRIBUTION_ENABLED=true`, log the engine’s
      `auto_high` set next to the DM’s actual panel decisions. Disagreements are
      the calibration corpus, and the measured `auto_high` precision gates Phase 2.

**Exit criteria to proceed:** engine `auto_high` precision ≥ 0.95 on the
benchmark, and on the shadow corpus the engine’s `auto_high` set agrees with the
DM’s decisions at least as often as the current pipeline’s `auto` set while
covering strictly more utterances.

---

## 4. Phase 2 — the review (first user-visible change)

**Goal:** the DM answers questions; the old panel becomes the escape hatch.

### attribution-service

- [ ] **Question generators** (`app/questions.py`): the six kinds of §8.1 and the
      suppression rules of §8.3, in the campaign language.
- [ ] **Ranking** (`app/ranking.py`): the objective and score of §9, including the
      greedy simulation that produces `questions_planned`.
- [ ] **Propagation** (`app/propagate.py`): §10, emitting `propagation_events`.
- [ ] API: the review endpoints of §15.4; events `attribution.computed`,
      `attribution.answered`, `attribution.review.completed`, and
      `attribution.review.ready` replacing `speaker.pending`.

### session-service

- [ ] Drive the new statuses; keep `speaker_assignments` as a derived view.
- [ ] Keep `/speakers/{label}/assign` and `/confirm` working — they become
      *direct attributions* that write `user_answer` evidence for the voice
      identity, so the escape hatch feeds the same engine.

### apps/web

- [ ] **Extract** the inline speaker UI from
      `app/campaigns/[id]/sessions/[sessionId]/page.tsx` into components. Pure
      refactor, no behaviour change, shipped first so the diff stays reviewable.
- [ ] Add `Modal`, `CoverageBar`, `ProvenanceChip`, `AttributionPicker`.
- [ ] `SessionReviewCard` + `ReviewFlow` + `QuestionCard` (§3, §4 of the UX doc).
- [ ] `VoicesPanel` with `Split` / `Merge` (§6).
- [ ] Reorder the page and collapse the transcript (§2).
- [ ] Export `Segment` / `TranscriptDoc` from `components/session/transcript.tsx`
      (they are currently module-local, so nothing can consume them) and move
      them into the SDK.
- [ ] Add `attributing` / `attribution_ready` / `attribution_review` to
      `SESSION_STATUS_TONE` in `components/ui.tsx` and to the `SessionStatus`
      union in the SDK.
- [ ] Add an **unassign** path. There is none today: nothing can clear an
      assignment or move `auto -> pending`, so a DM who answers wrongly would be
      stuck. The `AttributionPicker` needs it, and so does the legacy panel.
- [ ] Replace the `Confirm all` client-side loop with a single bulk operation
      (the engine-side equivalent is a `user_answer` evidence write over a voice
      identity, not N POSTs).
- [ ] Retire `LOW_SPEAKER_CONFIDENCE = 0.8` in `transcript.tsx` and the unused
      `systemApi.speakerModel()` threshold; chip styling follows the attribution
      status.
- [ ] Extend the SDK (`libs/typescript/dnd-sdk/src/index.ts`) with the review and
      attribution types; the SDK is the shared contract and must lead the web work.

### notification-service

- [ ] `attribution.review.ready` becomes the DM notification, with the coverage
      and the “about N questions” line instead of a label count.

**Exit criteria:** a DM can complete a real session’s review by answering ≤ 3
questions, the audio playback works from every question, and `Finish anyway`
produces the same downstream result as today.

---

## 5. Phase 3 — downstream consumption and the wiki gate

**Goal:** uncertainty changes what reaches the wiki.

### attribution-service

- [ ] Emit `transcripts/{session_id}/attributed.json` (§14.1) and
      `GET /internal/attribution/{id}/transcript`.

### content-service

- [ ] `app/workers/generate.py`: read the attributed artifact instead of
      `transcript.json` + `speakers.identified`; delete `build_speaker_map`,
      `resolve_speaker_names`, `party_character_names` and `_dm_speaker_names`
      in favour of the roster carried by the artifact. **The fallback to
      `player_name` disappears with them** — that is the fix for the “character
      page named after the player” leak.
- [ ] `app/chunking.py`: `build_view_lines` renders `Aramil`, `Aramil?`,
      `(unattributed)` per status and emits the `[u_XXXXX]` reference.
- [ ] `app/prompts.py`: `PROMPT_VERSION = v12` with `source_refs` on every item
      and `actor` on events, plus the two new rules (uncertain speakers may be
      quoted but not attributed; unattributed content stays at party level).
- [ ] `app/merger.py`: the attribution gate of §14.4 and the `SPEAKER_\d+` guard;
      **assertable in a unit test** — a fact whose `source_refs` do not resolve
      to a confident status must not reach a character page.
- [ ] `app/planner.py`: drop unresolved actors from event `participants` and from
      timeline `characters` instead of copying the model’s guess.
- [ ] The controlled backward edge for “Refresh the summary with the improved
      attribution”. Note `REVIEWABLE_STATUSES = {"summary_ready", "failed"}` in
      `app/api/summary_review.py` — it must gain `wiki_plan_ready` for the edge to
      be reachable, and that widening is what the "never after the change set is
      applied" rule depends on.
- [ ] Raise `max_chunks_per_session = 16` (`app/core/config.py`). The `[u_XXXXX]`
      reference on every view line adds roughly 2.5 tokens per line; on a 4-hour
      session that is ~1-2 extra chunks, and exceeding the cap raises `ValueError`
      and fails the job. Check the real chunk count on a long session before
      shipping the view change.

### apps/web

- [ ] Provenance chips in `session-summary.tsx` and the unattributed block.
- [ ] Click-to-seek from every chip and every timeline entry.

**Exit criteria:** the new merger gate has tests; end-to-end on a real session, no
wiki artefact contains a raw label or a player’s real name, and every fact traces
to `source_refs`.

---

## 6. Phase 4 — campaign-level learning

**Goal:** session *n+1* needs fewer questions than session *n*.

- [ ] `member_voice_models` accumulates per-voice-mode centroids; top-k scoring.
- [ ] `member_capabilities` accumulates from answers and from confirmed history.
- [ ] `attribution_calibrations` refit per campaign once enough labels exist.
- [ ] The measured false-confident rate is surfaced on the campaign settings page
      and auto-tightens the thresholds when it exceeds 3%.
- [ ] Optional: retro-**raise** confidence across the campaign’s unpublished
      sessions after a confirmation (§10.8).
- [ ] Reporting: questions per session over time, coverage per session, propagation
      yield — the numbers that prove the design works.

---

## 7. Phase 5 — retirement

- [ ] Remove `speaker.pending`, the `speaker_pending` status, the legacy
      `speakers.identified` verdict fields, `SpeakerAssignment` writes and the
      `speaker_assignments` table (replaced by the derived view).
- [ ] Remove the refiner’s speaker handling entirely.
- [ ] Update `docs/architecture.md`, `docs/data-model.md`,
      `docs/event-contracts.md` to describe the new model (this redesign becomes
      the documented architecture rather than a parallel one).

---

## 8. Migration and compatibility

| Surface | During the transition | At retirement |
|---|---|---|
| `speaker_assignments` table | kept, **derived** — never written directly | dropped |

One hazard makes the derived view a correctness requirement rather than
tidiness: `upsert_assignments()` overwrites `status` unconditionally but only
replaces `user_id` / `confidence` when the incoming value is non-`None`. A
redelivered `transcription.completed` / `transcription.refined` therefore
**downgrades `confirmed` rows back to `auto` / `pending` while leaving the
previous run’s `user_id` attached to them** — the DM’s work is silently undone
and the row becomes internally inconsistent. During the transition the
compatibility layer must not call that path at all; the engine is the only
writer, and the view is a projection of the current belief.
| `GET /api/sessions/{id}/speakers` | derived from voice identities | removed (use `/attribution` and `/voices`) |
| `POST .../speakers/{label}/assign`, `/confirm` | kept; they write `user_answer` evidence | removed |
| `speakers.identified` payload | gains `evidence`, keeps `speakers[]` | `speakers[]` removed |
| `speakers.assigned` | kept, still enrolls (with the quality gate) | removed |
| `speaker.pending` event | `attribution.review.ready` published alongside | removed |
| `speaker_pending` status | accepted as an alias of `attribution_review` | removed |
| `transcript.json` artifact | still written (raw/audit) | still written — it is the raw artifact |
| `diarization.json` artifact | still written; no longer rewritten by the LLM pass | unchanged |

Backfill: existing sessions are **not** re-attributed. The engine can compute an
attribution for a past session on demand, but re-running the wiki phase would
duplicate what the campaign documents, so it is off by default and exposed only
as an explicit admin action.

---

## 9. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The identity-evidence pass is unreliable at the real transcript quality | medium | high | it degrades gracefully: every channel is soft, and the engine falls back to voice + continuity only; the benchmark measures the pass in isolation |
| Belief propagation does not converge on messy sessions | medium | medium | damped updates, mean-field fallback, `converged` recorded and surfaced |
| Ranking is too slow on 4-hour sessions | low | medium | surrogate population of ≤ 400 utterances; ranking only on a new answer |
| The DM trusts an incorrect `auto_high` and it reaches the wiki | low | high | the corroboration rule (no single-channel `auto_high`), the measured false-confident rate, the 3% auto-tightening |
| “I don’t know” becomes the default answer out of fatigue | medium | medium | the question kinds decay (`P_uninformative`), two in a row ends the review, budget capped at 8 |
| Voiceprints stay bad because nobody enrolled | medium | high | the mobile “Tag a voice” flow; `who_is_voice` questions create prints from answers |
| Scope: this is a large change touching five services | high | medium | the six phases are independently shippable; the flag keeps the old path live |
| The engine and the compatibility view disagree | medium | low | the view is derived, never written; the engine is the only writer |

---

## 10. Open questions for the product owner

1. **Budget.** Default `MAX_QUESTIONS = 8`. Is 3 the acceptable median, and is 8
   an acceptable worst case for a first session?
2. **When to interrupt.** Should the DM be asked to review immediately after
   processing, or is the review a task they pick up whenever? (The design assumes
   the latter: the session rests on `attribution_review` and notifies.)
3. **Retro-attribution.** Should improving an earlier session’s attribution be
   allowed to touch its summary? The design allows it only before the change set
   is applied; extending it to published sessions needs an explicit decision.
4. **Guests.** Should a “someone not in the campaign” answer create a persistent
   guest member the DM can name later, or stay anonymous for the session?
5. **Capability sheets.** The engine works without them, but a DM-filled party
   sheet (class + signature abilities) would cut the first-session question count
   substantially. Is asking the DM to fill five fields once acceptable?
6. **Player-facing visibility.** Should players see the coverage figure and the
   attribution status of the lines they read? (Assumed: no — players see the
   published wiki, not the plumbing.)

---

## 11. Suggested first increment

If the whole plan is too much to start at once, the smallest increment that
tests the central hypothesis — *“one answer resolves many moments”* — is:

1. Phase 0’s `voice_observations` + multi-centroid models (speaker-service only,
   no new service);
2. the utterance builder and the voice channel, in a scratch script;
3. **one** question kind (`who_is_voice`) asked in the existing speaker panel,
   with the propagation measured and logged;
4. the benchmark on one session.

That is roughly a week of work, touches one service plus the web page, and
produces the number — *utterances resolved per answer* — that decides whether the
rest is worth building.

---

## 12. Implementation status

This section is the honest ledger: what is built, what is verified by tests, and
what is deliberately left for later. The design above is unchanged; this records
how much of it exists in the repository.

### Built and verified

| Area | Where | Tests |
|---|---|---|
| Shared clustering primitives (cosine, UPGMA, spherical k-means, ARI) | `libs/python/dnd_common/dnd_common/clustering.py` | `dnd_common/tests/test_clustering.py` |
| Cluster purity + merge decisions (the S5.1 test, bootstrap stability) | `libs/python/dnd_common/dnd_common/purity.py` | `dnd_common/tests/test_purity.py` |
| Chunk-boundary hazard in `speaker_turns` | `dnd_common/transcript.py` (`chunk_boundaries`) | `dnd_common/tests/test_transcript.py` |
| Per-observation audio quality (policy + SNR/overlap extraction) | `services/speaker-service/app/quality.py` | `tests/test_quality.py` |
| Per-turn observations, deterministic point ids | `services/speaker-service/app/observations.py` | `tests/test_observations.py` |
| Voice evidence contract (multi-centroid, candidate keys, no verdicts) | `services/speaker-service/app/evidence.py` | `tests/test_evidence.py` |
| `voice_observations` / `member_voice_models` Qdrant stores | `services/speaker-service/app/qdrant.py` | covered via the worker tests |
| Enrollment gates (quality + purity, member-keyed, userless members) | `app/workers/identify.py::_enroll_observations` | `tests/test_worker.py` |
| The anchor-path threshold defect (S12.3 defect 2) | `app/identify.py::anchor_verdict` | `tests/test_worker.py` |
| Text-only refinement (`REFINER_SPEAKERS=false`) | `services/refiner-service/app/prompts.py`, `refine.py` | `tests/test_refine.py`, `tests/test_worker.py` |
| Prompt-version drift (compose pinned v2, code declared v3) | `docker-compose.yml`, `app/core/config.py` | `tests/test_refine.py` |
| Attribution statuses + the retired blocking gate | `services/session-service/app/status.py` | `tests/test_status.py` |
| Derived `speaker_assignments` projection (one writer, no stale fields) | `app/services/sessions.py::project_assignments` | `tests/test_speaker_confirm.py` |
| The utterance builder (provenance, sub-splitting by word timings) | `services/attribution-service/app/utterances.py` | `tests/test_utterances.py` |
| Vectorised UPGMA, pinned against the reference (found a real diagonal bug) | `app/clustering.py` | `tests/test_clustering.py` |
| Split/merge detection: the five triggers, local re-diarization spans | `app/structure.py` | `tests/test_structure.py` |
| The eleven evidence channels | `app/channels.py` | `tests/test_channels.py` |
| Fusion, damped loopy BP, mean-field fallback (found a directed-message bug) | `app/inference.py` | `tests/test_inference.py` |
| The five statuses + the corroboration rule + coverage | `app/statuses.py` | `tests/test_statuses.py` |
| Calibration: KDE score→LLR, channel-weight fit, auto-tightening | `app/calibration.py` | `tests/test_calibration.py` |
| The six question kinds + the eight suppression rules | `app/questions.py` | `tests/test_questions.py` |
| Ranking: expected global entropy reduction, greedy plan, penalties | `app/ranking.py` | `tests/test_ranking.py` |
| Propagation: clamp, voice-model update, capability learning, split/merge | `app/propagate.py` | `tests/test_propagate.py` |
| The review loop and the stopping criterion | `app/review.py` | `tests/test_review.py` |
| The identity-evidence pass (prompt, schema, tolerant parser, view) | `app/evidence.py` | `tests/test_evidence_pass.py` |
| Capability store + compatibility solver | `app/capabilities.py` | `tests/test_capabilities.py` |
| The end-to-end pass + the attributed artifact | `app/pipeline.py`, `app/attribution_artifact.py` | `tests/test_pipeline.py` |
| DDL + migrations | `app/models.py`, `migrations/versions/0001_initial.py`, `0002_review_snapshot.py` | — (schema, not logic) |
| Review + internal APIs, the worker, the queue topology | `app/api/`, `app/workers/compute.py`, `dnd_common/events.py` | `tests/test_pipeline.py` |
| The wiki gate (status-based, raw-label guard, no player-name fallback) | `services/content-service/app/attribution.py` | `tests/test_attribution_gate.py` |
| The attributed view + prompt v12 (`source_refs`, `actor`) | `chunking.py`, `prompts.py` | `tests/test_prompts.py`, `test_attribution_gate.py` |
| The review UI, the demoted panels, the collapsed transcript | `apps/web/components/session/review.tsx`, `voices-panel.tsx`, `attribution.tsx`, the session page | typechecked (`tsc --noEmit`, `next build`) |
| SDK: attribution + review types, the new session statuses | `libs/typescript/dnd-sdk/src/index.ts` | typechecked |
| Batched message passing, pinned to the original loops (converged AND fallback) | `app/inference.py` | `tests/test_inference_batched.py` |
| Stored questions rebuilt with the engine's own keys | `app/services/review.py::rebuild_questions` | `tests/test_stored_questions.py` |
| Client route contracts + the public route surface | `app/clients/`, `app/api/` | `tests/test_clients.py`, `tests/test_api_routes.py` |
| Qdrant stores create what they read | `services/speaker-service/app/qdrant.py` | `tests/test_qdrant_stores.py` |
| Re-entry for a redelivered job (a crash must not strand a session) | `services/session-service/app/status.py` | `tests/test_status.py` |
| Re-drive attribution by hand | `services/attribution-service/scripts/recompute.py` | — (operator tool) |
| The post-pass event decision (ask, or announce that there is nothing to ask) | `app/workers/compute.py::review_event` | `tests/test_worker_assembly.py` |
| The summary phase's trigger, per mode | `services/content-service/app/workers/generate.py` | `tests/test_worker.py` |
| Review API authentication + campaign membership | `services/attribution-service/app/deps.py` | `tests/test_api_routes.py` |
| Per-voice guess, measured by what the engine will ACT on (verdicts), not by the posterior | `app/api/review.py::_voice_guesses` | `tests/test_api_routes.py` |
| The legacy Speakers panel is not offered while the engine is on | `apps/web/.../page.tsx` (`showSpeakersCard`) | typechecked |

### Bugs the tests caught, and why that matters

The equivalence test between the vectorised and reference UPGMA found that the
fast path merged every cluster **with itself** (the distance matrix's zero
diagonal won the `argmin`). The symmetry test in the inference suite found that
belief propagation was sharing ONE message per undirected edge, which makes a
symmetric two-node graph come out asymmetric — messages must be directed. Both
are the class of error that produces plausible-looking wrong answers in
production, and both were caught before any of it ran on a real session.

### The first real session: five defects, and what found them

The engine ran against a real four-hour recording for the first time on
2026-02-13. Nothing about it worked, and none of the five failures were visible
from the tests — that is the whole point of this list.

| # | Defect | Symptom | What found it |
|---|---|---|---|
| 1 | `plan_session` indexed `corroboration(...)` unconditionally, but it returns `None` whenever no non-voice channel is strong enough — the COMMON case, by design | every job died with `TypeError: 'NoneType' object is not subscriptable` and landed in the DLQ | the worker log; now a regression test that reproduces the exact traceback |
| 2 | The review router was mounted at `/api/sessions/{id}`, which belongs to session-service | the DM saw "Not Found" on three endpoints, from a service that had never heard of attribution | the gateway route table |
| 3 | The wiki client called `/internal/campaigns/{id}/characters` — a route no service has ever served | the capability channel silently ran empty | the 404 in the worker log |
| 4 | The Qdrant read paths never created their collections | `member_voice_models` 404'd on every search, logged as "search failed" for what is really "nobody has enrolled yet" | the speaker-service log |
| 5 | `infer` ran a Python loop per directed edge per sweep | ONE inference took 17.6 s on a 406-utterance graph; the ranking needs thousands, so the worker burned 100 % CPU for two hours and never finished | `docker stats` plus a stopwatch |
| 6 | `content-service`'s "may the summary phase start?" gate still listed only the OLD statuses (`speakers_identified`, `failed`) | the DM pressed **Finish review** and nothing happened; the session stayed on `attribution_review` for ever | the session page; the skip line in the content-service log |
| 7 | the same gate let `speakers.identified` start the summary with the engine ON | content-service and the engine both subscribed to it, so they RACED for the session and the loser was silently dropped | code review, prompted by 6 |
| 8 | the review API had no authentication at all | `Bearer x` was answered with 200, where every other service answered 401 | the route probe used to debug defect 2 |
| 9 | a session attribute-ready with nothing to ask had no event that could leave that status | it sat on `attribution_ready` until the DM opened the page and pressed Finish on a review with no question in it | code review, prompted by 6 |
| 10 | the legacy per-label Speakers panel was still live with the engine ON, and the compatibility projection that was supposed to back it was never written | the DM could confirm `SPEAKER_00 = Alice` — the exact claim the redesign exists because of — and it changed nothing, because the wiki gate reads the attributed transcript and never that table | the product owner asking "shouldn't this be obsolete?" |
| 11 | `_snapshot_with` wrote a different shape than `encode_belief`: the roster and the handles were dropped on every answer | after answering one question, every voice lost its name and read "Someone not in the campaign" | the voices panel showing the fallback label for all seven voices |
| 12 | a recompute rebuilds the belief from scratch, discarding `answers` / `voice_answers` / `learned` | `attribution.recompute` — a documented, supported operation — silently deletes the DM's answers, the most expensive evidence in the system; `review_runs.questions_asked` still counts them, so the run and the belief disagree | re-driving the session during this work. **Not fixed** |

Defects 1–4 are ordinary bugs. Defect 5 is the one worth recording: the
simulation-based objective is the design's central claim, and at session scale it
was three orders of magnitude too expensive **as literally specified**. Batching
the message passing (17.6 s → 0.07 s, verified equivalent to the original loop to
1e-9 on both the converged and the fallback path) plus a shortlist for the
simulation is what makes it affordable. Both the speed and the bound are now
stated in the code rather than assumed.

### The three that only the numbers could find

After the crash and the hang were fixed the session *completed* — and produced
nothing. Coverage 0.000, every utterance `unresolved`, zero questions planned.
Three separate defects were hiding behind the successful exit code:

- **Stakes-mismatched entropy.** The ranking's baseline `H(X_U|E)` was measured
  with per-utterance stakes; the simulated `H(X_U|E,a)` was measured without
  them. The objective is a DIFFERENCE, so every gain went negative and was
  clamped to zero — indistinguishable from "there is nothing worth asking".
- **Stored questions lost their targets.** `review_questions` stores utterance
  and voice IDs; the engine works in refs and handles. Returning the ids made
  every stored question *inert*: the answer was written onto a key no node had,
  so answering it changed nothing. The DM would have been shown questions whose
  answers did not matter.
- **The mean-field fallback was flattened.** `infer` ran `_normalise` (which
  exponentiates LOG-beliefs) over the mean field's output, which is already a
  probability distribution. A node that should have come out at 0.9996 came out
  at 0.28, so nothing could clear the `auto_high` bar. This only bites on the
  fallback path — which is the path a real session takes, because loopy BP does
  not settle on a 400-utterance graph.

Fixing all three moved coverage from **0.000 to 0.231** and the plan from
**0 questions to 1**, on the same audio.

A fourth showed up in the same measurement and is a *product* defect rather than
an implementation one: the stopping rule read "unresolved stakes" as the stakes
mass of the `unresolved` STATUS, not of everything the wiki gate will refuse.
A session where every moment was `auto_low` therefore reported zero unresolved
stakes and the review declared itself finished — asking the DM nothing, with
77 % of the session unattributed. That is the opposite of what the review is for.

### Not built

- **The hand-labelled benchmark** (plan §3). Two real sessions labelled at
  utterance level, the offline replay harness and the fake DM. This is the
  deliverable that turns "we are confident" into a measured number, and it needs
  real audio: it cannot be manufactured in a repository. Until it exists, the
  target metrics in §17 are targets, not claims.
- **`member_capabilities` / `member_voice_models` accumulation across
  sessions** (Phase 4). The tables and the writers exist; the campaign-level
  refit and the reporting are not wired to a scheduled job.
- **The retro-raise of earlier unpublished sessions** (§10.8) and the
  campaign-settings false-confident-rate surface.
- **Phase 5 retirement.** `speaker_pending`, the legacy verdict fields and the
  `speaker_assignments` upsert path are all still present, deliberately: the
  plan retires them one release AFTER the new path has been measured, and it has
  not been measured yet.
- **The mobile "Tag a voice" flow** (`docs/attribution-ux.md` §11).
- **Live end-to-end verification against real services.** The engine has now
  run against ONE real session (406 utterances, 7 voices, coverage 0.231 after
  the fixes above). That is one session on one campaign with no enrolled
  voiceprints and no DM capability sheet, so it exercises the degraded path, not
  the intended one. It is evidence that the pipeline runs and persists; it is
  not evidence that it attributes accurately.

### What must happen before the flag is turned on

1. Run the benchmark (plan §3) and check the `auto_high` precision target.
2. Set `REFINER_SPEAKERS=false`. The engine consumes the diarizer's labels as
   measurements; a label the LLM guessed is not a measurement.
3. Run one session end to end with the flag on and read the shadow comparison:
   the engine's `auto_high` set next to the DM's own panel decisions.

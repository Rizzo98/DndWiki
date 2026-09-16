# Speaker attribution — architecture & model redesign

> Companion documents: `docs/adr/0002-evidence-first-speaker-attribution.md` (the
> decision), `docs/attribution-ux.md` (the review experience),
> `docs/attribution-plan.md` (rollout, evaluation, risks).

This document supersedes §6 of `docs/architecture.md` and the
`speaker_assignments` section of `docs/data-model.md`.

---

## 0. The reframe

The current pipeline asks, once per session:

> *Which campaign member is `SPEAKER_00`?*

That question has no correct answer. `SPEAKER_00` is a **diarization
cluster**, i.e. a guess about which audio belongs together produced by a
model that merges adjacent speakers and splits the same speaker across
clusters. Treating the answer as an identity is the root cause of every
downstream failure — mis-attributed dialogue, wrong `participants` lists,
voiceprints enrolled from a mixture of two people, and a DM who has to
review a wall of `SPEAKER_XX -> member` rows.

The redesign asks, once per **moment**:

> *Given the voice, the moment in the conversation, what is being said, who
> else is present, what has already been confirmed, and what this campaign’s
> characters can do — who most likely produced this utterance, and how sure
> are we?*

Three consequences follow immediately:

1. **Identity is probabilistic and per-utterance.** A cluster is a grouping
   of *evidence*, not a person, and cluster-level attribution is a derived
   aggregate.
2. **The DM is asked about content, not plumbing.** “Who cast Fireball?”,
   never “confirm `SPEAKER_03`”.
3. **One answer is worth many.** Because the answer updates a voice model
   used to score *every* utterance, one click typically resolves hundreds of
   moments. The review is an optimisation problem — pick the question with
   the highest expected reduction of total uncertainty — not a checklist.

---

## 1. Why the current design cannot be patched

| # | Symptom | Where it lives today |
|---|---|---|
| 1 | **A cluster is assumed to be exactly one person.** The unique key is `(session_id, speaker_label)` and the value is a single `member_id`. | `services/session-service/app/models.py::SpeakerAssignment` |
| 2 | **One person is assumed to be exactly one cluster.** UPGMA over per-turn ECAPA-TDNN embeddings cuts at a fixed cosine distance (default `relabel_merge_threshold = 0.5`, i.e. similarity 0.5) or at a fixed `k`; both split a speaker who changes volume/microphone distance and merge two who do not. | `services/speaker-service/app/relabel.py::upgma_cluster` |
| 3 | **The decision uses one channel.** Auto-assignment fires on a single pooled centroid cosine `>= 0.75`. No capability, addressee, continuity or self-identification evidence enters. | `services/speaker-service/app/identify.py::match_label`, `app/core/config.py::speaker_match_threshold` |
| 4 | **The DM is many voices by construction — and the prompt enforces it.** The refiner is explicitly told to never create separate labels for different NPCs voiced by the DM, so one `SPEAKER_XX` is *guaranteed* to hold several people’s voices. This is not a diarization accident; it is the documented design. | `services/refiner-service/app/prompts.py` (`PROMPT_VERSION = "v3"`), `campaign_members.role = 'dm'` |
| 5 | **The pipeline blocks on total coverage.** `speaker_pending` is left only when *every* cluster is `confirmed`; an `auto` match counts as unconfirmed. A single bad cluster stalls the summary. | `services/speaker-service/app/workers/identify.py::_finish` |
| 6 | **The LLM is used to guess identities it cannot know.** The refiner pass rewrites speaker labels turn by turn from text alone — it never hears the audio, so its labels look like facts and are not. | `services/refiner-service/app/refine.py` |
| 7 | **Uncertainty is destroyed at the boundary.** `speaker_confidence` exists on segments but is dropped before generation; `content-service` receives only a name map and renders `[HH:MM:SS] NAME: text`. | `services/content-service/app/workers/generate.py::resolve_speaker_names`, `chunking.build_view_lines` |
| 8 | **Unresolved speakers leak into the wiki as entities.** An unmatched label stays `SPEAKER_00` in the view; `is_generic_name('SPEAKER_00', 'character')` is false and it is not a narrator name, so the model can emit — and the merger keeps — a character named `SPEAKER_00`. | `services/content-service/app/merger.py` |
| 9 | **Enrollment from a poisoned window.** Naming a speaker enrolls the *pooled* window of the whole label, including any segments in it that belong to someone else — permanently degrading every future session. | `services/speaker-service/app/workers/identify.py::process_assigned` |

Points 1, 2, 4 and 9 are all the same mistake seen from four sides. Adding a
threshold, a better embedding or another LLM pass does not fix it.

---

## 2. The five layers

```
  L0  audio segment        start/end + words, produced by ASR+diarization
      |                    immutable, never carries identity
      v
  L1  utterance            maximal run of consecutive same-label segments,
      |                    bounded by silence (>1.5 s) and chunk boundaries;
      |                    THE UNIT OF ATTRIBUTION
      v
  L2  voice identity       an anonymous, session-scoped voice fingerprint
      |                    ('V1', 'V2', ...). A grouping of evidence, not a
      |                    person. Splittable and mergeable at any time.
      v
  L3  player               a campaign member (may lack a user account).
      |                    The DM is a member whose voice identity set is
      |                    unconstrained (narrator + NPCs).
      v
  L4  character            who that member plays; the wiki-facing identity.
                           NEVER the player name on a page.
```

Orthogonal to the chain, two more objects carry the semantics:

- **Utterance semantics** — `kind` (action / dialogue / decision / narration /
  meta / backchannel), `voice_mode` (pc_dialogue / npc_dialogue / narration /
  ooc), `gist`, `stakes` (0..1: does the wiki care?), and the extracted
  `capability_requirements`.
- **Evidence** — every observation that raises or lowers a candidate’s
  probability, stored append-only with its own likelihood ratios.

| Layer | Cardinality | Identity? | Mutable? | Lives in |
|---|---|---|---|---|
| L0 segment | 100 s per session | no | no | `transcripts` bucket (MinIO) |
| L1 utterance | 1000s per session | no | no (text is; boundaries are stable) | `dnd_attribution.utterances` |
| L2 voice identity | ~2–20 per session | **anonymous** | yes — split/merge/rescope | `dnd_attribution.voice_identities` |
| L3 player | campaign roster | yes | no | `dnd_campaigns.campaign_members` |
| L4 character | 1 per player + NPCs | yes | via wiki pages | `dnd_wiki.wiki_pages` |

**The load-bearing rule:** an utterance’s attribution is a distribution over
L3 candidates (plus `unknown`). An L2 voice identity has a distribution too —
it is the *aggregate* of its utterances — and utterances may **defect** from
their voice identity whenever utterance-level evidence is decisive (a
capability that only one member has, a self-identification, an explicit
address). This is what makes “one cluster contains three people” and “one
person is three clusters” both representable without special cases.

---

## 3. Data model

### 3.1 New service, new database

A new **`attribution-service`** owns `dnd_attribution`. Rationale:

- `speaker-service` currently owns **no** database (it writes into
  `dnd_sessions.speaker_assignments` through an internal API), so the review
  state has nowhere to live inside the existing boundary.
- Evidence fusion and question generation are a distinct bounded context from
  embedding extraction; the former changes weekly, the latter rarely.
- `speaker-service` becomes a *measurable provider* (quality, similarity,
  recall) that can be swapped without touching the inference or the review.

`speaker-service` keeps: ECAPA-TDNN embeddings, audio windowing, Qdrant
read/write, multi-centroid per-member voice models, per-observation quality.
`speaker-service` loses: `match_label`, `status: auto`, the `pending` verdict,
`speaker_assignments` writes, the `transcription.refined` shortcut, and the
app/core/config.py thresholds that encoded policy.

### 3.2 Schema (PostgreSQL, `dnd_attribution`)

```sql
-- ---------------------------------------------------------------- utterances
CREATE TABLE utterances (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL,
    campaign_id       uuid NOT NULL,
    ordinal           int  NOT NULL,          -- chronological order
    start_sec         numeric(10,3) NOT NULL,
    end_sec           numeric(10,3) NOT NULL,
    gap_before_sec    numeric(10,3),
    text              text NOT NULL,
    language          text,
    -- provenance: which segment indices this utterance was built from
    segment_indices   int[] NOT NULL DEFAULT '{}',
    -- diarization: EVIDENCE, never identity
    diar_label        text,                   -- original SPEAKER_XX
    diar_chunk        int,                    -- source chunk (labels restart per chunk)
    diar_confidence   numeric(5,4),
    asr_confidence    numeric(5,4),
    -- L2 link (nullable: unembedded / noisy / overlapped utterances)
    voice_id          uuid REFERENCES voice_identities(id) ON DELETE SET NULL,
    voice_quality     numeric(5,4),           -- 0..1 SNR/overlap/length score
    -- semantics from the evidence-extraction pass
    kind              text,                   -- action|dialogue|decision|narration|meta|backchannel
    voice_mode        text,                   -- pc_dialogue|npc_dialogue|narration|ooc
    gist              text,
    stakes            numeric(5,4) NOT NULL DEFAULT 0.5,
    capability_reqs   jsonb NOT NULL DEFAULT '[]',
    addressed_names   text[] NOT NULL DEFAULT '{}',
    claimed_names     text[] NOT NULL DEFAULT '{}',
    UNIQUE (session_id, ordinal)
);
CREATE INDEX ON utterances (session_id, start_sec);
CREATE INDEX ON utterances (session_id, voice_id);

-- --------------------------------------------------------- voice identities
CREATE TABLE voice_identities (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL,
    campaign_id       uuid NOT NULL,
    handle            text NOT NULL,          -- 'V1', 'V2' ... display only
    observation_count int  NOT NULL DEFAULT 0,
    speech_sec        numeric(10,2) NOT NULL DEFAULT 0,
    centroid          jsonb,                  -- small copy for debugging;
                                              -- the real vectors live in Qdrant
    purity            numeric(5,4),           -- P(single voice | observations)
    impurity_evidence jsonb NOT NULL DEFAULT '[]',
    scope             text NOT NULL DEFAULT 'session',  -- session|campaign
    status            text NOT NULL DEFAULT 'open',     -- open|split|merged|locked
    split_from_id     uuid REFERENCES voice_identities(id),
    merged_into_id    uuid REFERENCES voice_identities(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, handle)
);

-- --------------------------------------------------------------- evidence log
-- Append-only. Every row is one observation with its own likelihood ratios.
CREATE TABLE utterance_evidence (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    utterance_id      uuid NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
    session_id        uuid NOT NULL,
    channel           text NOT NULL,
    -- voice|continuity|addressee|capability|self_name|third_person
    -- |out_of_world|meta|user_answer|propagation|prior
    origin            text NOT NULL DEFAULT 'engine',  -- engine|dm|rule
    source_ref        text,                   -- question id / rule id / evidence id
    payload           jsonb NOT NULL DEFAULT '{}',
    log_lr            jsonb NOT NULL DEFAULT '{}',     -- {candidate_key: log LR}
    weight            numeric(5,4) NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON utterance_evidence (utterance_id);
CREATE INDEX ON utterance_evidence (session_id, channel);

-- ------------------------------------------------------ current belief (cache)
-- Recomputed by the engine; fully derivable from the evidence log.
CREATE TABLE utterance_attributions (
    utterance_id      uuid PRIMARY KEY REFERENCES utterances(id) ON DELETE CASCADE,
    session_id        uuid NOT NULL,
    posterior         jsonb NOT NULL,         -- {'member:<uuid>':0.82,'unknown':0.08,...}
    best_candidate    text,                   -- 'member:<uuid>' | 'unknown'
    best_member_id    uuid,
    best_character    text,
    confidence        numeric(5,4),
    margin            numeric(5,4),           -- p1 - p2
    entropy           numeric(6,4),           -- nats
    status            text NOT NULL,
    -- auto_high|auto_low|user_confirmed|propagated|unresolved
    decided_by        text,                   -- engine|question:<id>|rule:<id>|propagation
    revision          int NOT NULL DEFAULT 1,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON utterance_attributions (session_id, status);

-- ---------------------------------------------------------------- the review
CREATE TABLE review_questions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL,
    campaign_id       uuid NOT NULL,
    revision          int  NOT NULL,
    kind              text NOT NULL,
    -- who_did | who_said (who_is_voice/same_voice/different_voice/new_person/
    -- presence are RETIRED: stored rows stay as the record, and are dropped when
    -- a review loads - see S8.1)
    prompt_text       text NOT NULL,          -- 'Who cast Fireball on the three goblins?'
    hook              jsonb NOT NULL DEFAULT '{}',
    -- {quote, audio:{start,end}, voice_ids:[], candidates:[{key,label,why}]}
    options           jsonb NOT NULL DEFAULT '[]',
    target_utterances uuid[] NOT NULL DEFAULT '{}',
    target_voices     uuid[] NOT NULL DEFAULT '{}',
    expected_gain     numeric(8,4),           -- bits of global entropy reduction
    gain_detail       jsonb NOT NULL DEFAULT '{}', -- per-option expected entropy
    cost              numeric(4,2) NOT NULL DEFAULT 1,
    score             numeric(8,4),           -- expected_gain * stakes / cost
    status            text NOT NULL DEFAULT 'candidate',
    -- candidate|asked|answered|skipped|stale|failed
    asked_at          timestamptz,
    answered_at       timestamptz,
    answer            jsonb,                  -- {option_key, member_id, note, elapsed_ms, answered_by}
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON review_questions (session_id, status, score DESC);

CREATE TABLE review_runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL UNIQUE,
    campaign_id       uuid NOT NULL,
    status            text NOT NULL DEFAULT 'pending',
    -- pending|in_progress|paused|complete|waived
    questions_planned int,
    questions_asked   int NOT NULL DEFAULT 0,
    coverage_before   numeric(5,4),
    coverage_after    numeric(5,4),
    entropy_before    numeric(10,4),
    entropy_after     numeric(10,4),
    engine_version    text,
    calibration_id    uuid,
    started_at        timestamptz,
    finished_at       timestamptz
);

-- ------------------------------------------------ what an answer changed (UX)
CREATE TABLE propagation_events (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL,
    question_id       uuid REFERENCES review_questions(id) ON DELETE SET NULL,
    resolved_utterances int NOT NULL DEFAULT 0,
    resolved_sec      numeric(10,2) NOT NULL DEFAULT 0,
    voice_ids         uuid[] NOT NULL DEFAULT '{}',
    learned           jsonb NOT NULL DEFAULT '{}', -- capabilities, voice models
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- -------------------------------------------------- campaign-level knowledge
CREATE TABLE member_capabilities (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       uuid NOT NULL,
    member_id         uuid NOT NULL,
    capability        text NOT NULL,          -- 'spell:fireball', 'class:wizard', ...
    polarity          text NOT NULL DEFAULT 'can',  -- can|cannot
    source            text NOT NULL,          -- dm|wiki|transcript|answer
    confidence        numeric(5,4) NOT NULL DEFAULT 0.5,
    session_id        uuid,
    evidence_count    int NOT NULL DEFAULT 1,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, member_id, capability, polarity)
);

CREATE TABLE member_voice_models (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       uuid NOT NULL,
    member_id         uuid NOT NULL,
    qdrant_point_id   text NOT NULL,          -- one centroid per row
    n_samples         int NOT NULL DEFAULT 0,
    mean_quality      numeric(5,4),
    source            text NOT NULL,          -- enrollment|session|answer|history
    session_id        uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON member_voice_models (campaign_id, member_id);

CREATE TABLE attribution_calibrations (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       uuid NOT NULL,
    model_version     text NOT NULL,
    params            jsonb NOT NULL,         -- channel weights, score->LLR maps
    n_labeled         int NOT NULL DEFAULT 0,
    fitted_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, model_version)
);
```

### 3.3 Qdrant

| Collection | Vector | Payload | Owner |
|---|---|---|---|
| `voiceprints` (existing) | 192-d ECAPA | `{campaign_id, member_id, user_id, source, session_id, sample_uri, quality, weight, version}` | speaker-service |
| `voice_observations` (**new**) | 192-d ECAPA | `{campaign_id, session_id, utterance_id, voice_id, start_sec, end_sec, quality, model_version}` | speaker-service |
| `member_voice_models` (**new**) | 192-d ECAPA | `{campaign_id, member_id, source, session_id, n_samples, quality}` | speaker-service |

`voice_observations` is what makes **purity analysis** possible: the per-turn
vectors of one cluster are the evidence for “this cluster is two people”.
`member_voice_models` is what makes **one player = many clusters**
representable: a member is scored against the best (or a top-k aggregation) of
several centroids, not against one averaged vector that represents none of
their voices well.

Retention: `voice_observations` is deleted with the session (the existing
session-delete path already purges recordings and artifacts). Session-derived
rows in `member_voice_models` carry `session_id` so they can be purged too.

### 3.4 What remains in `dnd_sessions`

`speaker_assignments` is **kept as a compatibility view** for one release: it
is derived from `voice_identities` + `utterance_attributions`
(one row per voice identity, `confidence` = aggregate posterior, `status` =
`confirmed` when the identity’s dominant member came from a user answer or an
`auto_high` aggregate). The old endpoints keep working by reading the view;
they are removed in the release after.

## 4. The evidence model

### 4.1 Notation

For an utterance `u` and a candidate `c` in
`C = {member:m1 .. member:mN} ∪ {unknown}`:

- `x_u` — the hidden author of `u` (a single value from `C`);
- `X_U` — the joint assignment of every unresolved utterance;
- `E` — everything observed: audio, text, vote/roster data, answers so far;
- `LR_e(u, c)` — the likelihood ratio the evidence `e` assigns to `c`;
  `log LR > 0` means “more likely than chance”, `log LR < 0` means “less”.

Every channel contributes a vector of log-likelihood ratios over `C`; the
engine sums them. **Nothing is a hard veto** — there is no `-inf` anywhere
except an explicit DM statement, because all of our “impossible” knowledge is
itself inferred.

### 4.2 Channels

| Channel | Signal | Extracted from | Typical log LR shape |
|---|---|---|---|
| `voice` | ECAPA-TDNN similarity of the utterance window to the member’s voice model (multi-centroid, top-k) | `voice_observations` vs `member_voice_models` in Qdrant | **calibrated**: `log f_same(s)/f_diff(s)`, range ±6 |
| `self_name` | the speaker names *their own* character (“Aramil casts Shield”, “I, Aramil, …”) | evidence pass | +3.5 to the named member (first person), +2.0 for third-person self-narration (common at D&D tables, weaker but very frequent) |
| `capability` | the utterance requires something only some members have (“I cast Fireball”, “I rage”, “I sneak attack”, “I speak Draconic”) | evidence pass + `member_capabilities` + wiki character page `attributes.character.class` | +`w_cap · compat(c, req)` per requirement, `w_cap ≈ 1.2`, capped at ±3.0; `compat = -1` **only** when the campaign has a DM-authored capability sheet |
| `address` | the previous utterance names a member and this one follows within `handoff_sec` (“Thorin, what do you do?” → next turn) | evidence pass + timing | +2.5 to the addressed member, `-0.4` to the addresser |
| `narration` | second-person scene description, NPC voice, rules adjudication, scene framing | evidence pass (`voice_mode`) | +2.2 to the DM, spread over the other members |
| `ooc` | table meta-talk, snacks, scheduling, rules lawyering | evidence pass (`kind = meta`) | +0.8 to the DM, +0.3 to everyone (low information) |
| `roll` | dice rolls, initiative, “whose turn is it” | evidence pass | +1.0 to the member whose turn the previous adjudication assigned, else flat |
| `identity_note` | in-session meta facts that change the *candidate set*: “Keth’s player is in the bathroom”, “I’m playing Thorin tonight”, “Thorin isn’t here yet” | evidence pass (`identity_notes`) | sets a window during which a member’s prior is multiplied by 0.05, or transfers a member’s identity temporarily |
| `voice_mode` | `npc_dialogue` decided by the DM | evidence pass | +2.0 DM; **does not create an NPC entity by itself** — NPC naming stays a content-service concern |
| `dm_prior` | the DM speaks the most and initiates scenes | speech-share statistics of the session | +0.5·log(speech_share(c)/mean), capped at ±1.5 |
| `user_answer` | the DM answered a question about exactly this utterance | `review_questions.answer` | **clamps** the posterior (see §10.4) |

Why this set, and not more: each channel had to be (a) extractable without new
audio infrastructure, (b) independently verifiable in the transcript, and
(c) plausibly discriminative at a real D&D table. Channels that failed one of
those tests (prosody/sentiment, “who is the main character”, room acoustics) are
deliberately absent.

### 4.3 Fusion

Attribution is *not* a flat product over all utterances, because that would be
both intractable and wrong (utterances in one cluster are strongly coupled). It
is a **three-level hierarchy**:

```
  level 1   clustering      observation -> voice identity   (speaker-service)
  level 2   identity        voice identity -> member       (small: <=20 x <=8)
  level 3   defection       utterance -> member, overriding its identity
```

The potential of an utterance is

```
  phi_u(x)  =  prior(x) · VI_posterior(x)^alpha_u · PROD_e LR_e(u, x)
```

where `VI_posterior` is the posterior of `u`’s voice identity (level 2) and
`alpha_u ∈ [0, 1]` is how much the utterance trusts its cluster:

```
  alpha_u = purity(VI(u)) · quality(u) · cosine(e_u, centroid(VI(u)))
```

So an utterance at the edge of an impure cluster, or one whose own embedding is
far from the cluster centroid, is free to defect. A clean utterance in a pure
cluster is essentially glued to it. **This single formula is what makes “one
cluster, several people” work without a special case.**

The joint distribution adds two edge factors:

```
  P(X | E)  ∝  PROD_u phi_u(x_u)
             · PROD_(u,v) in same_voice   psi_sv(x_u, x_v)   -- (1-eps) if equal else eps
             · PROD_(u,v) in adjacent     psi_ct(x_u, x_v)   -- turn-taking prior
```

- `same_voice` edges connect utterances inside one voice identity (sparse by
  construction) **plus** cross-identity pairs whose observations are mutually
  more similar than `d_same_p90` — those edges are how a *merge* is discovered.
  Their strength is **the identity’s own purity**, not a constant:
  `eps = SAME_VOICE_EPS + (0.5 - SAME_VOICE_EPS) · (1 - purity)`, so a voice
  measured pure (purity 1.0) glues hard, a suspect one barely pulls, and an
  identity whose purity was **never measured** gets the neutral `eps = 0.5`,
  where “same person” and “different people” are equally likely and the factor
  cannot move a message at all. An unmeasured identity is not a verified one:
  clamping a hundred utterances together because a handful of them merely look
  like the narrator is how a session with seven distinct voices came out as
  `The Dungeon Master` for all 419 of its utterances.
- `psi_ct` encodes that consecutive turns alternate speakers, with a decay by
  gap length and a cancellation when `u` is a question addressed to a name.

### 4.4 Inference

Damped loopy belief propagation, 10 sweeps, deterministic initialisation from
`phi_u` alone. Convergence is monitored by the max change in any marginal; if it
does not converge in 30 sweeps the engine falls back to mean-field updates and
records `converged: false` on the run (a diagnostic, not a failure).

Complexity is dominated by the `same_voice` edges. Two engineering rules keep
it linear-ish:

1. **Within a cluster, connect each utterance to its `k = 8` nearest neighbours
   in embedding space, not to all pairs.**
2. **Question ranking runs on a surrogate**: the `M = 400` utterances with the
   highest `stakes` plus everything above the entropy floor. Exact inference
   runs over the full set once per answer, not once per candidate question.

---

## 5. Cluster purity: detecting that one label is several people (and vice versa)

A voice identity is a hypothesis, and the engine must be able to say *this
hypothesis is wrong*. Purity is computed from the identity’s own observations.

Purity is a **measurement, and `None` until one is taken**. It is never
defaulted to 1.0: “we did not check” and “we checked and it is one person” are
different claims, and every consumer of purity — the `same_voice` coupling of
§4.3, `alpha_u`, the *Voices we found* panel — has to be able to tell them
apart.

### 5.1 The test

Given the observations `{e_1..e_n}` of a voice identity (cap the sample at 200
by taking the longest/cleanest utterances):

1. **Calibrate the baseline once per campaign.** From confirmed assignments and
   enrollment samples, estimate the distribution of *within-speaker* pairwise
   cosine distances: `d_same_p50`, `d_same_p95`. This is the yardstick; the
   current code uses a global constant (`relabel_merge_threshold = 0.5`) that
   cannot know how similar two samples of *this* person’s voice are.
2. **Split into two** on the sphere (spherical k-means, 25 restarts) → `A`, `B`.
3. **Score the split:**
   ```
   sep    = 1 - cos(centroid_A, centroid_B)
   within = max(mean_intra(A), mean_intra(B))
   impure if  sep > within + margin
          and sep > d_same_p95
          and min(|A|, |B|) >= 3
          and min(speech_sec(A), speech_sec(B)) >= 10
   ```
4. **Stability**: bootstrap-resample 80% of the observations 20 times and re-run
   the split. The verdict is accepted only if the same partition recurs with
   `ARI >= 0.8` in at least 80% of resamples. A single unlucky clustering never
   triggers a split.

`purity` is stored as a continuous value (the fraction of stable resamples) so
downstream consumers can weigh it instead of reading a boolean.

### 5.2 Cheap triggers that raise the prior (then run the test)

Running the test on every identity is cheap; running it *early* on suspicious
identities makes the engine fast. Five triggers:

| Trigger | Why it works |
|---|---|
| **Anchor conflict** — observations inside one identity match two *different* enrolled members above the genuine threshold | Direct contradiction; the only way both can be right is that the identity holds two people |
| **Length outlier** — the identity’s longest turn exceeds the session p99 turn length | A four-minute “turn” at a table is almost never one person |
| **A-B-A pattern** — within the identity’s span the original diarizer label changed and changed back within 3 s | The classic artifact of a diarizer merging two speakers who alternate |
| **Capability contradiction** — the identity’s utterances require capabilities no single member has (`spell:fireball` **and** `feature:rage`) | Semantic evidence that the audio contradicts itself |
| **Confidence collapse** — the identity contains segments whose `speaker_confidence` is far below the session median | The diarizer itself was unsure there |

### 5.3 Resolution: local re-diarization, not a full re-run

When an identity is found impure, the engine does **not** re-run ASR. It takes
the identity’s time span plus 2 s of padding and re-processes *only* that span:

1. sliding windows of 1.5 s with 50% overlap, embedded by speaker-service;
2. agglomerative clustering **with the campaign-calibrated cut** instead of a
   global threshold;
3. re-cut the affected utterances at the new boundaries (an utterance that
   straddles two voices is split into two utterances, preserving the text by
   word timings);
4. new identities get `split_from_id = <the impure identity>`.

The cost is a few seconds of audio per impure identity, not a per-session
re-transcription — which is why this can run on CPU and inside the review loop.

### 5.4 The mirror problem: one person, several clusters

Two identities are proposed for **merging** when

```
  dist(centroid_A, centroid_B) < d_same_p50
  and neither has contradicting anchor evidence
  and their utterances do not overlap in time (a person cannot be in two
      places at once) -- unless the identity is the DM, who can
```

Merges are cheap (they only re-point `utterances.voice_id`) and are *always*
confirmed by propagation math before being applied: if merging increases the
total log-likelihood of the observed evidence, merge; otherwise do not. The DM
sees a merge only when the engine is genuinely undecided (see `same_voice`
questions in §8).

### 5.5 The DM is multi-modal *by design*

The engine must never try to force the DM into one voice identity. The DM’s
`member_voice_models` row set is expected to grow with every NPC voice, and the
`narration` + `voice_mode` evidence channel is what carries the *decision* that a
given voice is “the DM doing an NPC” rather than a party member. Concretely:

- a voice identity with no plausible party match, whose utterances are mostly
  `npc_dialogue` or `narration`, gets a strong DM prior;
- the DM’s voice model is scored with `top-k` over its centroids and is allowed
  to match *any* of them, so “the DM doing a goblin” and “the DM narrating” do
  not compete with each other;
- the UI never asks “is this the DM?” when the `narration` evidence is decisive.

## 6. Extracting identity-bearing content from a D&D transcript

### 6.1 What we extract, and what we do not

The wiki extraction in `content-service` (`PROMPT_VERSION = v11`) answers
“what is worth documenting?”. Attribution needs a different, earlier, cheaper
question: **“which moments tell us who was speaking?”**. Those are not the same
set. “The party bought rope” is wiki-worthy but says nothing about identity;
“I rage and attack the captain” is identity-decisive but may never reach a wiki
page.

So: a dedicated **identity-evidence pass** over the diarized, text-corrected
transcript, run **before** the content pass, on the cheap model, with a small
output. It never rewrites the transcript and never assigns names.

### 6.2 The view we send

Each utterance is rendered with a stable reference the model must echo back:

```
Roster (player -> character):
  Alice -> Aramil (Wizard)          Bob   -> Thorin (Fighter)
  Carla -> Keth (Rogue)             Dan   -> Elara (Cleric)
  DM    -> (narrator)
Known character abilities (from the campaign sheet and earlier sessions):
  Aramil: class:wizard, spell:fireball, spell:shield
  Thorin: class:fighter, feature:second_wind

[u_00410 00:41:02 V3] Bob: ok so I move up to the door
[u_00411 00:41:07 V1] DM: as you push it open you see three goblins and a
                          bigger one in a captain's coat
[u_00412 00:41:15 V4] Carla: I cast fireball on the three goblins
[u_00413 00:41:19 V2] Bob: wait, do I still get my attack?
```

The `V3`/`V1` markers are the *current anonymous voice identity*, shown to the
model on purpose: it lets the model say “u_00411 and u_00420 are the same voice
and both narrate”, which is evidence text alone would miss. The model is
explicitly told these are anonymous voice groups, **not** people.

### 6.3 The output schema

```json
{
  "language": "en",
  "utterances": [
    {
      "ref": "u_00412",
      "kind": "action",
      "voice_mode": "pc_dialogue",
      "gist": "casts Fireball on the three goblins",
      "capability_requirements": ["spell:fireball"],
      "claims": [{"type": "self_character", "value": "Aramil", "strength": 0.9}],
      "addresses": [{"name": "Thorin", "target": "next_turn"}],
      "stakes": 0.85
    }
  ],
  "identity_notes": [
    {"type": "member_absent", "member": "Keth",
     "from": "u_00390", "to": "u_00450",
     "reason": "Carla said she was stepping out"},
    {"type": "delegation", "from_member": "Carla", "to_member": "Bob",
     "from": "u_00510", "to": null,
     "reason": "Bob is running Keth for the rest of the night"},
    {"type": "voice_group_hint", "voices": ["V1", "V6"],
     "note": "both narrate the scene"}
  ]
}
```

`kind`, `voice_mode` and `stakes` are ordinal/enum-like; `claims`, `addresses`
and `capability_requirements` are the discriminative payload. The schema is
small on purpose: the model is doing classification and mention-detection, not
summarisation, and a small schema is what keeps this pass cheap and reliable.

### 6.4 The D&D evidence taxonomy

The prompt enumerates the table-talk patterns that actually identify a speaker.
This list *is* the domain knowledge of the system:

| Pattern | Example | What it proves |
|---|---|---|
| First-person action | “I cast Fireball on the three goblins.” | the actor is speaking; the capability constrains who |
| Third-person self-narration | “Aramil moves to the door and checks for traps.” | **very common at real tables** — the speaker narrates their own character; strong, but weaker than first person |
| Self-naming | “I, Aramil, step forward.” / “my character Aramil” | near-decisive |
| Summoning by name | “Thorin, what do you do?” | the *next* turn is Thorin |
| Capability marker | “I rage”, “I sneak attack”, “I cast Shield”, “I speak Draconic” | restricts the candidate set to members with that capability |
| Item possession | “I draw my longsword” / “I open my spellbook” | maps to `item:`/`weapon:` capabilities learned over time |
| Narration | “You enter a wide hall; the torches gutter.” | the DM |
| NPC voicing | “The innkeeper says: you’ll find him at the docks.” | the DM, in `npc_dialogue` mode |
| Rules adjudication | “That’s a DC 15, roll with disadvantage.” | the DM |
| Turn call | “Thorin, your turn.” / “whose go is it?” | the named member acts next |
| Table meta | “I’m going to get a drink”, “can we rewind?” | `ooc`, low stakes, usually not the DM |
| Absence / delegation | “Keth isn’t here tonight”, “Bob is running Keth” | changes the candidate set for a time window |
| Dice | “I rolled a 19 plus five.” | weak; confirms “same speaker as the last action” |

### 6.5 Chaptering and cost

A 4-hour session is ~35–45k tokens. The pass runs per chunk of ~8k tokens
(larger than the content pass’s 4k, because the output per chunk is tiny), with
4-way concurrency, `temperature = 0`, and a hard cap of 40 utterances per chunk
in the schema description. Expected cost is on the order of one extra content
pass — and it *replaces* work the refiner does today: with this design the
refiner pass is reduced to **text correction only** (ASR errors, spellings,
proper names) and stops emitting speaker labels entirely.

### 6.6 What the pass is explicitly forbidden to do

- assign or change speaker labels;
- invent members not in the roster;
- emit a raw diarization label as a name;
- resolve an actor by guessing “probably the wizard” — capability requirements
  are recorded as *requirements*, and the engine decides.

---

## 7. Uncertainty: statuses, calibration and honesty

### 7.1 The five statuses

| Status | Rule | Used by the wiki? | Shown as |
|---|---|---|---|
| `user_confirmed` | the DM answered a question covering this utterance | **yes** — character-level facts allowed | solid name, green |
| `auto_high` | `p_max >= 0.90` **and** `margin >= 0.50` **and** (a non-voice channel with `log LR >= 1.5` **or** `voice log LR >= 3.0`) | **yes** | solid name, neutral |
| `propagated` | inferred from an answer rather than answered: `p_max >= 0.95` **and** `margin >= 0.75` | **yes**, reported separately | name + a small link icon |
| `auto_low` | `p_max >= 0.60`, below `auto_high` | session-scope text only, **never** a character-page fact | name + `?`, amber |
| `unresolved` | anything else | no — party-level wording or omission | grey “someone” |

The corroboration requirement in `auto_high` is deliberate: **a single channel
never yields high confidence.** A bare cosine of 0.76 does today, which is
exactly how silent errors reach the wiki.

`propagated` is stricter than `auto_high` because it generalises from an answer
instead of observing the moment. The distinction matters: if the DM later says a
propagated attribution is wrong, the blast radius is the propagation rule, and
the engine can invalidate the rule rather than distrust the DM.

### 7.2 The confidence number must be earned

Three mechanisms:

1. **Voice score to log LR.** Fit `f_same(s)` and `f_diff(s)` by Gaussian KDE
   from the campaign’s confirmed history (`speaker_history`, `voiceprints` with
   `source in (enrollment, session, history)`) and map
   `log LR(s) = log f_same(s) - log f_diff(s)`. Cold start (no history) uses a
   logistic `a*(s - s0)` where `s0` and `a` come from the dispersion of the
   campaign’s own enrollment samples.
2. **Channel weights.** Fit the eight channel weights by logistic regression on
   the DM’s answers plus the confirmed history. A campaign with ~300 labelled
   utterances has enough data; below that, use the module defaults.
3. **Report the residual.** The engine stores, per campaign, the measured
   **false-confident rate** = the share of `auto_high` attributions the DM later
   contradicted. Above 3%, the `auto_high` thresholds are automatically
   tightened for that campaign. The number in the UI (“94% identified”) must be
   backable by this measurement, not asserted.

### 7.3 Never let a guess become a fact

Three hard rules, enforced in code rather than in a prompt:

- **Status monotonicity.** `unresolved` < `auto_low` < `auto_high` <
  `propagated` < `user_confirmed` in what they may write. A lower status can
  never be promoted by a downstream consumer.
- **No silent fallback.** Resolving a speaker must never fall back to the raw
  diarization label or to a player’s real name. With neither a confident
  character name nor an explicit role, the utterance stays unattributed.
- **Traceability.** Every generated fact carries the `source_refs` it came from,
  so an attribution can be traced back to the question or rule that produced it
  — in both directions.

---

## 8. Generating candidate questions

### 8.1 Two question kinds, both about a moment

Each kind is a *template* instantiated from the evidence graph. The DM never
sees a label or a cluster id.

| Kind | Trigger | Example | Cost |
|---|---|---|---|
| `who_did` | a high-`stakes` `action`/`decision` utterance with two or more candidates and entropy above the floor | “Who cast Fireball on the three goblins?” | 1.0 |
| `who_said` | a `dialogue` utterance that is quoted, decided upon or referenced later | “Who said they wanted to enter from the eastern passage?” | 1.0 |

Every question’s option list ends with **always** *I don’t know*; it offers the
plausible roster members in posterior order, rendered as
`Player — Character (Class)`, plus *Someone not in the campaign* and *The Dungeon
Master* when `p(narrator) >= 0.05`.

**Both other families are retired** (`questions.RETIRED_KINDS`), and both were
measured rather than argued. Stored rows are dropped when a review loads; the
rows stay in the database as the record of what was asked.

* **The voice-identity kinds** — `same_voice`, `different_voice`,
  `who_is_voice`, `new_person`. Measured against the DM’s own ear and lost: the
  diarization clusters they asked about are mixtures of several people, so “who
  is this voice?” had no true answer, and a wrong answer wrote a strong prior onto
  every moment of a cluster that was not one person.
* **The `presence` questions** — “Was Thorin there?” over a stretch. They
  replaced the voice kinds and lost on the DM’s second session, for a reason that
  is structural rather than a matter of tuning: **a scene holds almost everybody
  almost always**, so the answer was a foregone “yes”, and a question whose answer
  the reading already asserts buys nothing. Worse, being priced at half a click
  (§9.3) and spanning a whole stretch, it took the ranking by storm — on the
  session that killed it, **20 of the 24 simulated candidates were presence
  questions** (proxy 70.7 against 1.23 for the best moment question, a factor of
  57). Every question that could have settled a moment was never simulated, never
  asked and never shown, and the DM was left with a review made entirely of “Was
  X there?”.

The lesson is the one §12.6 now states positively: the readings the engine makes
about a session — where it happens, who is around — are **context**, and the
review’s job is the thing context cannot supply, which is *who did this*.

### 8.2 How a question is phrased

Questions are written in the **campaign language**, from the utterance `gist`,
using the table’s own words:

```
  who_did      ->  Who <gist>?
                   Who cast Fireball on the three goblins?
  who_said     ->  Who said: “<quote>”?
```

Every question carries a **hook**: the quoted text, what to play, and a one-line
*why*. The hook is what makes a question answerable in seconds instead of
minutes. What it plays is the thing the question is about — the line itself — and
a question with nothing concrete to point at is refused by the generator (§8.3
rule 8) rather than asked.

### 8.3 Suppression rules — the questions that must never be asked

A candidate question is discarded before ranking when:

1. **The engine already answers it** — the moment is in a CONFIDENT status
   (`auto_high`, `propagated`, `user_confirmed`), i.e. `p_max >= 0.90` and
   `margin >= 0.50` **and** the statuses were willing to promote it. (The “don’t
   confirm a speaker at 95%” rule, expressed as a number — and, per §7.1, a
   number is not enough: a lone weak channel never yields high confidence.) A
   peaked posterior the statuses refuse to promote is `auto_low`: ignorance the
   wiki will not act on, and therefore exactly what the review is for.
2. **A rule determines it** — capability or collapse logic resolves it, so it is
   resolved rather than asked.
3. **The subject was already covered** — the same voice identity, or more than
   50% utterance overlap with a question already asked.
4. **The stakes are too low** — `stakes < 0.15` and `p_max >= 0.5`: filler,
### 8.3 Suppression rules — the questions that must never be asked

A candidate question is discarded before ranking when:

1. **The engine already answers it** — the moment is in a CONFIDENT status
   (`auto_high`, `propagated`, `user_confirmed`), i.e. `p_max >= 0.90` and
   `margin >= 0.50` **and** the statuses were willing to promote it. (The “don’t
   confirm a speaker at 95%” rule, expressed as a number — and, per §7.1, a
   number is not enough: a lone weak channel never yields high confidence.) A
   peaked posterior the statuses refuse to promote is `auto_low`: ignorance the
   wiki will not act on, and therefore exactly what the review is for. Reading
   the raw posterior as knowledge instead is how a session that was never
   attributed produced one structural question and then went silent.
2. **A rule determines it** — capability or collapse logic resolves it, so it is
   resolved rather than asked.
3. **The subject was already covered** — the same voice identity, or more than
   50% utterance overlap with a question already asked or answered.
4. **The stakes are too low** — `stakes < 0.15` and `p_max >= 0.5`: filler,
   backchannels, cross-talk. These become `unresolved` and are rendered at party
   level. The DM is never asked about filler.
5. **The candidate set is degenerate** — zero or one plausible candidate, or a
   member already excluded for that window by an `identity_note`.
6. **The gain is below the floor** — `G(q) < GAIN_FLOOR` (default 0.15 bits).
7. **The budget is spent** — `MAX_QUESTIONS` (default 8) reached, or two
   consecutive *I don’t know* answers.
8. **The DM could not answer from memory.** If answering requires reasoning about
   a cluster abstraction, the generator refuses to emit the question.

Rule 8 is worth stating explicitly as a design constraint: **a question the DM
cannot answer from their memory of the session is a bug in the generator**, not
a hard question. The generator must always be able to point at a concrete
moment.

---

## 9. Ranking: expected reduction of total uncertainty

### 9.1 The objective

Let `U` be the unresolved utterances and let

```
  H(X_U | E) = SUM_{u in U}  stakes(u) * H(x_u | E)        (mean-field)
```

For a candidate question `q` with answer space `A_q` (the presented options,
*I don’t know* included):

```
  G(q) = H(X_U | E)  -  SUM_{a in A_q}  P(a | E) * H(X_U | E, a)
```

`H(X_U | E, a)` is **not estimated from a formula**: it is the entropy measured
after actually running the propagation of §10 for answer `a` and re-running
belief propagation. We ask the question whose *simulated* answer reduces total
uncertainty the most — precisely the “resolve as many other moments as possible”
property the product needs.

### 9.2 Why this is affordable

- The candidate pool is bounded by construction: one question per (stretch,
  member) pair — five stretches and six characters is thirty — plus one per moment
  the belief cannot settle. On a real 404-moment session: 131 candidates, of which
  the shortlist simulates 24.
- Each simulation is a handful of BP sweeps over a surrogate of ≤ 400
  utterances with mean degree ≤ 10 — single-digit milliseconds in NumPy.
- 60 candidates × ≤ 6 options × ~10 ms ≈ **a few seconds**, and it re-runs only
  when a new answer arrives, not on a timer.
- Greedy is near-optimal here: the objective is monotone submodular in the set of
  answered questions, so greedy achieves at least `1 − 1/e` of the optimal
  expected gain. A cleverer search would not be explainable to the DM anyway.

### 9.3 Score, cost, stakes, redundancy

```
  score(q) = G(q) * mean_stakes(target(q)) / cost(q)
             - rho * P_uninformative(q)
             - lambda * overlap(q, questions already asked)
```

- `cost` is the DM effort from §8.1 (0.7 for a binary audio A/B, 1.0 for a
  roster pick, 1.3 when listening is required).
- `mean_stakes(target)` keeps the review pointed at what the wiki will use.
- `P_uninformative(q)` is this campaign’s measured rate of *I don’t know* for
  questions of that kind, so the kinds the DM keeps bouncing decay.
- `overlap` punishes redundancy with questions already asked.

### 9.3.1 The shortlist: how much a question TOUCHES

The objective above is *measured*, and measuring it costs a propagation per
option per candidate - hundreds of candidates on a four-hour session. So only
`SIMULATION_BUDGET` (24) candidates are ever scored, chosen by a cheap stand-in
that has to be right about one thing above all: **how many moments a question is
about.** The stand-in is the stakes-weighted uncertainty the question touches, in
the objective's own units:

```
  reach(q) = SUM over u in target(q) of  stakes(u) * H(x_u)
  proxy(q) = reach(q) / cost(q)
```

and `target(q)` is the question's **own** moments if it names any, otherwise the
moments of the voices it names. Both halves of that are corrections, and both
were real bugs:

- **the sum, not the entropy of the mean.** `H(mean posterior)` answers "how
  undecided is a typical moment", which is the same number whether a question
  covers one moment or a hundred. A question that settles an entire voice
  identity therefore scored no better than one that settles a single line, and
  lost the tie on cost and mean stakes. Measured on a real session: the seven
  the seven questions about a voice ranked **121-129 of 131** against a budget of
  24, so they were never simulated - and once `reach` put them in the running they
  won by a factor of forty, which is how the DM discovered that the clusters they
  asked about were mixtures of several people. (Those questions are now retired,
  §8.1.)

  The same proxy later let the `presence` questions do it in the other
  direction: at cost 0.5 and a reach of a hundred and sixty-seven moments they
  took 20 of the 24 slots, and the review the DM saw was “Was X there?” and
  nothing else. Retiring them (§8.1) is what restores the budget to the moment
  questions; the ordering below is unchanged, and it is still the *measured*
  gain, not the proxy, that decides who is asked.
- **own moments XOR voices, never the union.** Every question carries a voice -
  the identity of the moment it is about - so adding the cluster to a question
  that already names a moment credits a single-line question with the reach of
  the whole cluster it sits in. In the first version of this fix that is exactly
  what happened, and the voice questions lost again *because* the single-line
  questions were asking for the same forty moments at cost 1.0 against 1.3.

The proxy still cannot see the knock-on effect an answer has on moments *outside*
the question, nor whether the answer settles its targets or merely reshapes them
(a "different people" answer changes the coupling, not the labels). That is what
the simulation is for: **the proxy decides who competes, never who wins**, and it
is never reported to the DM as a gain. The bound is stated plainly rather than
hidden - a question the proxy ranks below the budget is never asked.

That bound is also the reason a question kind can only be judged on a whole
session: a proxy that likes a cheap, wide question will starve every other kind
out of the simulation, and since only simulated questions are ever ranked, the
starvation is invisible from inside the ranking. It shows up in the *kinds the DM
is asked*, which is where both retirements in §8.1 were caught.

### 9.4 "Answer approximately 2-3 questions to finish"

**The DM is no longer told a number of questions.** There is no honest number to
tell: the length of a review is a function of the answers, because every answer
moves the belief and the stopping rule of §11 reads the state that produces.

What the **greedy simulation** measures is a *best case*: starting from the
current posterior, repeatedly take the argmax option under the current belief
instead of asking, propagate, and count how many questions it takes to reach the
stopping criterion. That is the shortest review possible under the assumption
that the DM answers every question the way the evidence points — an assumption
reality is free to break, and broke:

| | questions |
|---|---|
| simulation at compute time | 6 |
| questions the DM actually answered | 8 |
| simulation re-run on the state those answers produced | 8 (the whole budget) |

The session ended with 84 % of what matters still unattributed. So the card
showed “about 6 questions to finish”, then “8 of 6 answered”: two numbers, both
artefacts of asking a question that has no answer. Three corrections:

- **no estimate is displayed.** `GET /review` returns `plan.answered`,
  `plan.max_questions` and `plan.budget_spent` — two facts and a flag — and the
  card says “question 3 of 8”, or “all 8 questions answered”. The simulation is
  still computed (once per revision: it is a propagation per option per
  shortlisted candidate, tens of seconds on a real session) and still returned in
  the run block as a diagnostic, and it still decides whether a review exists at
  all (§15.1). It is simply not presented as progress.
- **the bar is a state, not a countdown.** It reads “we are confident about N % of
  your session”, and the line under it says how much of what matters is *still*
  unattributed. Both come from the same classification the wiki gate uses (§11.1).
- **a review is a fixed number of questions, not a completion procedure.** With
  `MAX_QUESTIONS = 8` and a session that needs far more, the review is a sampling
  of the DM’s knowledge; the coverage it buys is real but partial, and saying so
  is the whole point of §11.2.

---

## 10. Propagating one answer

An answer is not a local patch; it is a global update. In order:

### 10.0 What counts as “propagated”

An answer moves a set of moments: the answered one (which is `user_confirmed`,
not inferred), the moments its evidence reaches, and the ones the structural or
capability rules touch. `Propagator.measure` records **exactly those** in
`Belief.propagated_refs`, by comparing the posteriors on either side of the
answer.

A moment nothing moved — and that nobody answered — keeps its own observed
verdict. This is not a detail: `propagated` is deliberately stricter than
`auto_high` (§7.1), so applying it to the whole session as soon as any answer
existed demoted every corroborated moment onto the higher bar. Answering
questions could then only ever push the session’s confidence **down** — a real
session went from 15.24 % identified to 14.70 % after eight correct answers,
which is the progress bar the DM watched move backwards.

**The record has to survive the round trip.** `propagated_refs` is part of the
belief, and the snapshot is the only thing the review reads: a belief written
without it comes back as “unknown” and falls back to the conservative reading,
so an answered session reads *worse* than an untouched one, on every page load.
That is exactly what happened — the answer path kept its own copy of the belief
encoder and the copy had lost the key. Two defences:

- `services.review.encode_belief_state` is the **single** encoder, used by the
  compute pass and by every answer (`api.review._snapshot_with`). Two copies of
  a data contract is how this broke; there is now one.
- a snapshot that arrives without the record (written by an older engine) is
  **repaired on load**: `propagate.moved_by_answers` asks the whole-session
  version of the question `measure` asks per answer — take every answer away,
  infer, and see which posteriors move. It is the weaker claim of the two (a
  moment that only moved in the presence of a later answer is not in the set),
  it costs one extra inference, and it is the honest one: it names exactly the
  moments the DM’s answers are currently responsible for. Measured on the session
  above: 12.41 % as read with the fallback, 15.82 % with the derived set (and
  18.07 % if the answers were simply ignored, which would be a lie in the other
  direction).

### 10.1 Clamp the target

The answered utterance(s) and, for voice-kind questions, the voice identity get
a point-mass evidence row (`channel = user_answer`, `weight = 1`). For a
`same_voice`/`different_voice` answer the effect is structural instead (§10.4).

### 10.2 Update the member’s voice model — *this is the big one*

The answered utterance’s observations are added to `member_voice_models` for the
chosen member as a **new centroid** (or merged into an existing one within
`d_same_p50`), with `source = answer` and a quality weight. Then:

- every utterance in the session is re-scored against the updated models;
- the voice channel’s log LRs change for *all* of them at once;
- a member with several centroids is now matched with top-k aggregation, so their
  other voice modes are recognised too.

One answer therefore re-scores the entire session in a single vector operation.
That is what produces the “this also helped us identify 12 other moments”
message — and 12 is a conservative example: answering a `who_is_voice` question
for a talkative player typically moves dozens to hundreds of utterances.

### 10.3 Learn a capability

If the answered utterance carried `capability_requirements`, they are written to
`member_capabilities` for the chosen member with `source = answer` and high
confidence. From then on **every other utterance requiring that capability is
resolved or strongly biased**, including in future sessions. This is the cheapest
possible generalisation: “Player 3 cast Fireball once” is permanently useful
knowledge about that character.

### 10.4 Re-scope the structure (split / merge)

- “**Different people**” on a `same_voice` question forces the split of that
  identity at the boundary the answer implies, re-runs local re-diarization
  (§5.3) inside the span, and marks both halves pending fresh evidence.
- “**Same person**” on a `different_voice` question merges the two identities:
  re-point `utterances.voice_id` and union the observations.
- Both are recorded as `propagation_events` with a full audit trail.

### 10.5 Run the graph

Re-run belief propagation over the utterance graph. Adjacency and continuity
edges move the neighbours of the answered utterance; capability edges move every
utterance sharing the learned requirement; the updated voice channel moves
everything else.

### 10.6 Re-classify and measure

Recompute `status` for every utterance and record the deltas:

```
  resolved_utterances, resolved_sec, voice_ids, learned{}
```

This is exactly the payload of `propagation_events` and of the “also resolved N
other moments” message. Nothing here is decorative: the count is the number of
utterances that moved from `auto_low`/`unresolved` to
`auto_high`/`propagated` because of this one answer.

### 10.7 Contradiction handling

If an answer contradicts overwhelming voice evidence (the utterance is 0.99
similar to member A’s model and the DM said member B), the engine:

1. honours the answer for the answered utterance — a DM can be right about a
   guest using someone else’s headset, or about their own memory;
2. does **not** let the answer override the voice model globally: the new
   centroid is added with reduced weight and the conflict is recorded;
3. records a `contradiction` diagnostic on the run and, when the campaign’s
   contradiction rate is high, flags the member’s voice model as possibly
   polluted — a bad enrollment print is the usual cause.

Silently averaging the two would destroy both. These asymmetries are the
difference between a system that learns from corrections and one that drifts.

### 10.8 Cross-session propagation (deliberately bounded)

`member_voice_models` and `member_capabilities` are campaign-scoped, so the next
session starts with everything learned. In addition, on confirmation the engine
may **raise** the confidence of `auto_low`/`unresolved` utterances in *earlier,
not-yet-published* sessions of the campaign. It never downgrades a
`user_confirmed` attribution and never rewrites a published session.

---

## 11. When to stop

### 11.1 Coverage, defined on stakes rather than labels

Today’s UI cannot report a meaningful percentage: “3 of 5 labels confirmed” says
nothing about how much of a four-hour session is actually understood. The
redesign reports:

```
  coverage = SUM  stakes(u) * speech_sec(u) * confident(u)
             ------------------------------------------------
             SUM  stakes(u) * speech_sec(u)

  confident(u) = status(u) in {user_confirmed, auto_high, propagated}
```

This is the number the DM sees (“We identified 94% of your session”) and the
number the pipeline acts on.

### 11.2 The stopping criterion

The review stops when **all** of the following hold:

1. `G(q) < GAIN_FLOOR` for every candidate question — no remaining question is
   worth the DM’s time; **and**
2. unresolved stakes mass is below `TARGET_UNRESOLVED = 0.10` **and** every
   extracted event with `stakes >= 0.7` has an attributed actor — **or**
3. the DM hits the budget, answers *I don’t know* twice in a row, or presses
   **Finish**.

Condition 2 is the important one: a session is not “done” while the moments that
will become wiki pages are still unattributed, even if the aggregate number looks
good. Coverage is a *summary*; the stopping rule is driven by what the wiki will
consume.

### 11.3 What happens to what is left

Leftover `unresolved` utterances are **not** a failure state:

- excluded from character-page facts and from event participant lists;
- rendered at party level in the session summary (“the party forced the eastern
  door”) or dropped;
- listed in a collapsed “N moments we could not attribute” block with a one-click
  path back into the review, so the DM can revisit them later;
- the session can still be confirmed and published — the pipeline no longer
  blocks on full speaker coverage.

## 12. The existing voice recognition: keep it, demote it, harden it

Automatic voice recognition from pre-recorded player audio is the single most
valuable signal available, and it stays. What changes is its **role** and its
**contract**.

### 12.1 It stops being the decision

Today `match_label` turns a cosine into `auto | pending` at a hard threshold
(`SPEAKER_MATCH_THRESHOLD = 0.75`) and the whole session parks on
`speaker_pending` until the DM validates every label. That is a policy decision
buried in an ML pipeline. After the redesign, `speaker-service` returns
**evidence**:

```
observation -> { nearest_centroids: [{member_id, centroid_id, cosine, quality}],
                 model_version, calibrated: {...} }
```

and the attribution engine converts cosine to a log-likelihood ratio using the
campaign’s calibration (§7.2). The threshold 0.75 disappears as a policy; it
becomes one point on a fitted score-to-LLR curve.

### 12.2 It becomes multi-centroid, not single-centroid

`_load_anchors` currently averages **all** of a member’s prints into one
centroid. For a member with several voice modes (shouting in combat, whispering
at the table, speaking through a bad laptop mic) the average represents none of
them and pushes every mode toward the middle. The redesign keeps each print as
its own centroid (`member_voice_models`) and scores a member by the best (or a
top-3 mean) match. This is the direct fix for “one player, many clusters”.

### 12.3 Enrollment gets quality gates

Three defects in the current enrollment path, all of which poison future
sessions:

1. **`process_assigned` embeds the pooled window of a whole label.** If the label
   contained two people, the enrolled print is a mixture. Fix: enroll **per
   observation**, only observations whose `status` is `user_confirmed` or
   `auto_high` **and** whose identity `purity >= 0.9` **and** whose own quality is
   above the floor.
2. **The anchor path bypasses the match threshold.** In `_match_relabeled` a
   cluster snapped to an anchor is emitted as `status: auto` with the anchor
   cosine and **never consults `speaker_match_threshold`**, so a 0.60-similarity
   snap auto-assigns where the direct path would have required 0.75. Fix: the
   engine consumes calibrated LLRs, so the inconsistency cannot exist.
3. **`upsert_assignments` overwrites `status` but keeps stale `user_id` and
   `confidence`.** Fix: the compatibility view derives everything from the current
   belief, so a stale field is impossible.
4. **Userless members can never be learned.** Enrollment keys on `user_id`, and
   `process_assigned` logs *“named without a linked user; skipping voiceprint
   enrollment”* and returns. A member without an account therefore never gets a
   print and can never auto-match in any later session, no matter how many times
   the DM names them. This is why `member_voice_models` is keyed on **`member_id`**
   and `user_id` is only ever a convenience link — the redesign has to fix this,
   and it is one of the cheapest wins in the whole plan.

Additional gate: an observation is rejected when overlap detection or the SNR
estimate flags it (`quality < 0.35`), when it is shorter than
`voice_sample_min_sec`, or when its identity is currently suspected impure. A
print is written with its `quality` and `weight`, and the engine can down-weight
it later without deleting it.

### 12.4 The DM’s voices are enrolled deliberately

The DM accumulates a centroid per NPC voice (`source = answer`, `voice_mode =
npc_dialogue`). They are searched as one member with many centroids, which is
exactly the semantics needed: “this is the DM again, doing another voice”.

### 12.5 What speaker-service still owns, and what it loses

| Keeps | Loses |
|---|---|
| ECAPA-TDNN embedding extraction and windowing | `match_label` and the `auto/pending` verdict |
| `voiceprints` collection read/write | `speaker_assignments` writes |
| `voice_observations` and `member_voice_models` collections | the `transcription.refined` short-circuit |
| per-observation quality (SNR, overlap, length) | `relabel_*` policy thresholds |
| the campaign history backfill (`history.py`) | deciding when the session may continue |

`relabel.py`’s UPGMA moves into the attribution engine’s clustering step, where
it can be seeded with **calibrated** cut points and used for **local** splits
instead of a global pass.

---

### 12.6 The scene reading: where the session happens, and who is there

A session is not one conversation in one place. The party moves, the scene cuts,
a player leaves the table for twenty minutes, and the table splits up mid-scene.
Until now the engine could express exactly one absence - an `identity_note`,
which needs somebody to SAY it out loud - so every moment was weighted against
every member of the campaign for the whole session.

`app.scenes` reads that structure from the session's own text, once, in one
call. What it produces per stretch: the **location**, the **characters there**,
the characters the record places **elsewhere**, the **NPCs** in play, and the
**event that started the stretch**.

Three things about it are deliberate, and each was measured rather than assumed:

1. **A stretch ends when somebody JOINS OR LEAVES, not only when the party
   moves.** This is the whole signal. A first version asked for "scenes" and put
   all six characters in all three scenes - which is true of a party that stays
   together and useless to the engine. Asking instead for *the stretches in which
   the same people are together* produced, on the same session: Letho absent from
   moment 178, Hann joining at 316, and 72 moments in which only two of the six
   are at the cart. Those boundaries are invisible to a summary and obvious to the
   DM, whose own words ("Letho si allontana", "il gruppo si divide") are quoted
   as the stretch's reason.
2. **It reads the TEXT, not the gists the evidence pass produced.** The gists are
   subject-less by design ("casts Fireball on the three goblins"), so a reading
   built on them put every character in every scene: the words that place somebody
   in a room are the words the gist pass takes out. One call over the session's
   text costs roughly what four evidence chunks cost.
3. **Only a STATED absence excludes anybody.** The engine applies the `absent`
   list as the same strong negative log LR as an `identity_note` (-3 nats), per
   moment. It does **not** turn the `present` list into a penalty by complement: an
   incomplete present list would silence the quiet player, who is precisely the
   person the review is trying to find. Presence is evidence about who is in the
   room, and only its explicit half is used.

Measured on a real 404-moment session: **228 moments (56%) lose at least one
candidate, covering 63% of the session's stakes-weighted speech** - and the
moment a character walks back in, he is a candidate again.

The reading is **persisted** in `session_scenes` (one row per stretch, one
revision), with the names exactly as read and the member ids they resolve to, and
served by `GET /api/attribution/sessions/{id}/scenes`. The DM reads it in the
*Where this session happens* panel, collapsed, next to the review. That panel is
not decoration: the engine excludes members, and an exclusion nobody can see is
an exclusion nobody can correct.

#### 12.6.1 The reading is context, not a question

The first attempt to make the reading *ask* something was the **presence
question** - "Is Hann in this stretch?", one click per (member, stretch) pair.
It is retired (§8.1), and the reason is worth keeping, because it is the same
reason a reading must never become a cast census: **a scene holds almost
everybody almost always.** On a real session the reading's `present` list was
the whole party in four stretches out of six, so the question had one answer, and
asking a question whose answer is already asserted costs the DM a click and buys
the belief nothing. The stretches where the question *was* informative are the
ones that state an absence - and measuring those showed the answer moves no moment
into a confident status either way.

So the reading keeps its three jobs, and all three are about **context**:

1. it **excludes** a member the table put elsewhere, at -3 nats per moment, the
   same strength as a stated absence (§12.6 step 3);
2. it is **shown** in the *Where this session happens* panel, so an exclusion
   nobody can see does not become an exclusion nobody can correct;
3. it **travels with the artifact** (`stretches`, §14.1) and so reaches the
   extraction prompt - which is what fixes the "un personaggio" beats. A narration
   is filed under the DM, so a beat about somebody else had no name to use; in a
   stretch where the reading puts exactly ONE party member present and names the
   others elsewhere, the writer is told that a party member acting or experiencing
   something in those lines IS that member, and to name them. It is the one place
   the reading can supply a subject the speaker cannot, and it is gated on the
   reading's most specific claim rather than on its default cast list.

What is still NOT built: a way for the DM to *dispute* an exclusion. Today a wrong
absence can be seen in the panel but corrected only by a recompute, which discards
the DM's answers - so on an answered session it cannot be corrected at all. The
presence question was meant to close that gap and did not: it was a question about
a cast, and the cast was never where this system was losing.

The reading is best-effort like the evidence pass: a failed call leaves the
session exactly as it was before this existed, with no presence evidence at all
(never with everybody absent).

---

## 13. Should the system still map speakers to players? (question 12)

**No — not as the source of truth.** The recommendation is to move to a
**segment/voice-cluster-level attribution model**, with three explicit positions:

1. **A diarization cluster is never an identity.** It is a grouping of evidence
   with a purity estimate. The model must be able to represent “this cluster is
   three people” and “this person is three clusters” at zero cost.
2. **A voice identity is anonymous and session-scoped.** `V1` is not a person and
   is never shown to the DM as one. It exists so that evidence can be pooled and
   so a split/merge has something to operate on.
3. **Cluster-to-player survives as a derived, aggregate view.** After inference,
   a voice identity’s owner is `argmax` of its utterances’ aggregate posterior.
   That view is what the compatibility layer exposes as `speaker_assignments`,
   and it is what a “Voices we found” panel can show — but nothing in the
   pipeline reads it as input.

Why this is not merely cosmetic: today the 1:1 key forces **every** consumer to
invent an answer where none exists. The transcript renderer invents one
(`SPEAKER_00 -> character_name`), the wiki extraction invents one (it will
happily write a character page for `SPEAKER_00`), the enrollment path invents
one (it enrolls a mixed print), and the DM is asked to invent one. Moving the
unit of truth to the utterance removes the invention from all four at once.

The one place a label-to-person mapping is still genuinely useful is **the DM’s
mental model during review**, and there the answer is not a mapping at all but
an audio hook: *play the voice*. The redesign replaces the abstraction with the
thing itself.

---

## 14. Downstream: the summary and the wiki

### 14.1 The attributed transcript artifact

A new artifact `transcripts/{session_id}/attributed.json` replaces the label
map as content-service’s input:

```json
{
  "session_id": "...", "language": "en",
  "attribution_revision": 7,
  "coverage": 0.94,
  "roster": [
    {"member_id": "...", "player_name": "Carla", "character_name": "Keth",
     "role": "player", "capabilities": ["class:rogue", "feature:sneak_attack"]}
  ],
  "utterances": [
    {"id": "u_00412", "start": 2475.0, "end": 2478.4,
     "text": "I cast fireball on the three goblins",
     "speaker": {"member_id": "...", "character_name": "Aramil",
                 "role": "player", "mode": "pc_dialogue"},
     "status": "user_confirmed", "confidence": 0.97,
     "kind": "action", "stakes": 0.85}
  ]
}
```

Content-service reads **only** this artifact. The existing `transcript.json`
stays as the raw/audit artifact behind the collapsed transcript view.

### 14.2 How each status is rendered into the prompt

| Status | `build_view_lines` output | Prompt consequence |
|---|---|---|
| `user_confirmed` | `[u_00412 00:41:15] Aramil: I cast fireball...` | none; treat as fact |
| `auto_high` | same | none; treat as fact |
| `propagated` | same | none; treat as fact |
| `auto_low` | `[u_00412 00:41:15] Aramil?: ...` | the `?` plus a prompt rule: facts stated *by* an uncertain speaker may be recorded, but must not be attributed to that character |
| `unresolved` | `[u_00412 00:41:15] (unattributed): ...` | prompt rule: never attribute this content to a named character; describe it at party level or omit it |

The view now carries the utterance reference `u_00412`, which the extraction is
required to echo back in a new per-item `source_refs` field. That single change
makes the whole pipeline auditable: every fact on every page can be traced to the
utterances that produced it, and from there to a timestamp and an audio span.

### 14.3 Schema additions

`EXTRACTION_SCHEMA` (v12) gains, on characters, locations, events and timeline
entries:

```json
"source_refs": {"type": "array", "items": {"type": "string"},
  "description": "The [u_XXXXX] ids of the utterances this item was derived from."}
```

and events gain an explicit actor:

```json
"actor": {"type": "string", "description":
  "The character who performed the event, exactly as named in the transcript;
   empty when the transcript does not attribute it."}
```

### 14.4 The gate that keeps wrong attribution out of the wiki

Enforced in `merger.py` / `planner.py`, not in a prompt:

```
  character page fact   requires source_refs resolve to user_confirmed | auto_high | propagated
  location session_refs requires the same -- `session_references[]` on a location page
                        carries attributed session facts too, not just character pages
  event participants    requires the same; unresolved actors are dropped from the list
  event actor           requires the same, else left empty
  timeline characters   requires the same
  session summary line  may use party-level wording ("the party ...") for unresolved content
  any page              may never contain a raw diarization label or a player's real name
```

The last line is a filter, not a hope: the merger gains an explicit
`SPEAKER_\d+` guard (today `is_generic_name('SPEAKER_00', 'character')` is false,
so the model can and does emit such entities) and the name resolver loses its
fallback to `player_name` — with no `character_name`, the speaker is
*unresolved*, not a player-named character page.

**Do not confuse the merger's existing `confidence` with attribution
confidence.** `merger.py::_entity_confidence` computes
`round(appearances / total_chunks, 3)` — it measures *cross-chunk agreement*,
not attribution certainty, and the LLM is never asked for it. The gate keys on
the attribution **status** of the `source_refs`, never on that number. A fact
repeated in twelve chunks has `confidence = 1.0` and can still be attributed to
the wrong character.

### 14.5 Re-generating after a later review

If the DM improves the attribution *after* the summary exists, the session page
offers **“Refresh the summary with the improved attribution”**. This is a
controlled backward edge, allowed only from `summary_ready` and
`wiki_plan_ready` (never once the change set has been applied, because
re-generating then would duplicate what the campaign already documents). The
state machine gains exactly one edge for it.

---

## 15. Service, API, events and state machine

### 15.1 Session state machine

```
  uploaded -> recorded -> transcribing -> transcribed -> refining -> refined
           -> identifying_speakers -> speakers_identified
           -> attributing -> attribution_ready
           -> attribution_review          (RESTING, skippable, non-blocking)
           -> summarizing -> summary_ready
           -> generating_wiki -> wiki_plan_ready -> applying_wiki
           -> content_ready -> reviewed -> published
```

- `speaker_pending` is **retired** as a blocking state. For one release it is
  accepted as an alias of `attribution_review` so in-flight sessions survive a
  deploy.
- `attributing` / `attribution_ready` are short; `attribution_review` is the
  resting state, identical in kind to `summary_ready` and `wiki_plan_ready`.
- `attribution_review -> summarizing` is the DM pressing **Finish** (or the
  engine reporting no useful question left). There is no path that keeps the
  session stuck waiting for the DM.
- `failed -> attributing` is the retry edge, consistent with the existing
  `failed -> summarizing | generating_wiki | applying_wiki` pattern.

### 15.2 Queue topology

| Queue | Binds | Consumer |
|---|---|---|
| `transcripts.refine` | `transcription.completed` | refiner-service (text only) |
| `speakers.identify` | `transcription.refined` | speaker-service (observations + similarity evidence) |
| `attribution.jobs` (**new**) | `speakers.identified`, `attribution.answered`, `attribution.recompute` | attribution-service |
| `content.generate` | `attribution.review.completed` (replacing `speakers.identified`, `speakers.assigned`) | content-service |

### 15.3 Events

Unchanged: `session.recorded`, `transcription.completed`, `transcription.refined`,
`speakers.identified` (now carrying evidence, not verdicts), `speakers.assigned`
(kept for one release), `content.*`, `wiki.*`.

New:

```jsonc
// attribution.computed - a pass over the session finished
{ "session_id": "...", "campaign_id": "...", "revision": 3,
  "coverage": 0.94, "unresolved": 41, "questions_planned": 3,
  "questions_asked": 1, "engine_version": "attr-1", "converged": true }

// attribution.answered - the DM answered one question
{ "session_id": "...", "campaign_id": "...", "question_id": "...",
  "kind": "who_did", "option": "member:<uuid>", "resolved_utterances": 12,
  "resolved_sec": 187.4, "coverage": 0.97, "learned": {"capabilities": 1,
  "voice_centroids": 1} }

// attribution.review.completed - the review is over (finished, skipped or waived)
{ "session_id": "...", "campaign_id": "...", "outcome": "finished",
  "questions_asked": 2, "coverage": 0.97, "unresolved": 12,
  "review_run_id": "..." }
```

`speaker.pending` is replaced by `attribution.review.ready`, whose payload is
`{campaign_id, session_id, coverage, unresolved, questions_planned}` — a message
the DM can act on (“about 3 questions”) instead of a label count.

### 15.4 Public API (behind the gateway)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sessions/{id}/review` | review status: coverage, buckets, planned questions, run status |
| GET | `/api/sessions/{id}/review/next-question` | the top-ranked question, or `null` when done |
| POST | `/api/sessions/{id}/review/answer` | `{question_id, option_key, member_id?, note?}` → `{resolved_utterances, coverage, next_question}` |
| POST | `/api/sessions/{id}/review/skip` | `{question_id}` — never ask this one again |
| POST | `/api/sessions/{id}/review/finish` | stop reviewing; emits `attribution.review.completed` |
| GET | `/api/sessions/{id}/attribution` | paginated per-utterance attribution (for chips and the transcript) |
| POST | `/api/sessions/{id}/utterances/{uid}/attribute` | power-user direct fix (`member_id` \| `unknown` \| `narrator`) |
| POST | `/api/sessions/{id}/voices/{vid}/split` | `{at_utterance_id}` — manual structural fix |
| POST | `/api/sessions/{id}/voices/merge` | `{voice_ids}` |
| GET | `/api/sessions/{id}/voices` | the “Voices we found” panel |

Internal (`/internal/...`, service-to-service): `POST /internal/attribution/{id}/compute`,
`GET /internal/attribution/{id}/transcript` (the attributed artifact),
`GET /internal/attribution/{id}/status`.

### 15.5 Configuration

```
ATTRIBUTION_ENABLED           = false   # kill switch, old path stays live
ATTR_AUTO_HIGH_PMIN           = 0.90
ATTR_AUTO_HIGH_MARGIN         = 0.50
ATTR_PROPAGATED_PMIN          = 0.95
ATTR_PROPAGATED_MARGIN        = 0.75
ATTR_GAIN_FLOOR_BITS          = 0.15
ATTR_TARGET_UNRESOLVED        = 0.10
ATTR_MAX_QUESTIONS            = 8
ATTR_PURITY_SPLIT_THRESHOLD   = 0.80
ATTR_LOCAL_REDIARIZE_WINDOW   = 1.5     # seconds
ATTR_EVIDENCE_CHUNK_TOKENS    = 8000
ATTR_EVIDENCE_MODEL           = <cheap model>
ATTR_ENGINE_VERSION           = "attr-1"
```

---

## 16. Guardrails and failure modes

| Failure | Detection | Response |
|---|---|---|
| **A bad enrollment print poisons the campaign** | the member’s prints have low mutual similarity (they do not look like one voice) | flag the member, exclude the outlier print from scoring, ask the DM to re-record — never silently keep it |
| **The DM answers wrong (fatigue)** | an answer contradicts an `auto_high` voice match | honour the answer locally, do not let it override the global voice model, surface the conflict on the review card |
| **Propagation over-reaches** | a `propagated` attribution is later contradicted | invalidate the specific propagation rule, downgrade the affected utterances, re-rank questions |
| **Capability inference is wrong** (“only the wizard can cast Fireball”) | a capability-derived attribution contradicts voice evidence | capability constraints stay soft (bounded log LR); only a DM-authored sheet can veto |
| **Question fatigue** | the DM skips or answers *I don’t know* repeatedly | two consecutive *I don’t know* ends the review; the `P_uninformative` penalty decays that question kind |
| **The engine disagrees with itself after a re-run** | revisions produce different `auto_high` sets | every attribution carries `revision`; conflicting revisions are resolved by status precedence (`user_confirmed` wins) and never by “latest wins” |
| **A session has no usable audio** | no observations | the engine degrades to a text-only pass (capabilities, self-naming, addressivity, DM narration) and reports a lower coverage; it never marks everything `unresolved` silently |
| **A guest or a second DM appears** | a voice identity that no roster member fits | `new_person` question; the answer “someone not in the campaign” creates a *guest* pseudo-member, and its utterances are excluded from character pages |
| **Cross-talk / overlap** | overlap detection on the window | the utterance is marked `unresolved` with a low weight; overlapping speech is never attributed to one person |
| **Cost blow-up** | pass token accounting | the evidence pass is chunked, capped at 40 items per chunk, and skippable per campaign |

---

## 17. Metrics that decide whether this worked

Product-level:

| Metric | Target | Why |
|---|---|---|
| **Questions per session** | median ≤ 3 on a 4-hour session with enrolled voiceprints | this is the entire promise |
| **Questions in the first session of a campaign** | ≤ 6 | cold start is the worst case and must still be tolerable |
| **Propagation yield** | ≥ 20 utterances resolved per answer | measures whether answers generalise |
| **DM review time** | ≤ 90 s end to end | seconds, not questions, is what the DM feels |
| **Coverage** | ≥ 0.90 stakes-weighted after review | drives the wiki gate |

Correctness:

| Metric | Target |
|---|---|
| Turn-level attribution accuracy vs. a hand-labelled benchmark | ≥ 0.92 |
| **False-confident rate** (`auto_high` later contradicted) | ≤ 0.02 — this is the safety property |
| Split/merge decisions correct | ≥ 0.85 |
| Wiki facts whose `source_refs` are unresolved | 0 (hard gate, assertable in a test) |

The benchmark is not optional: a hand-labelled ground truth for two or three
real sessions at utterance level is the only way to claim “minimum questions”
without hand-waving. `docs/attribution-plan.md` specifies how to build it.

---

## 18. Summary of the changes

| Concern | Today | After |
|---|---|---|
| Unit of attribution | diarization label | utterance, with a posterior |
| Voice cluster | assumed = one person | anonymous identity with a purity estimate |
| Player mapping | 1:1, hard | distribution, plus utterance-level defections |
| Evidence | pooled cosine vs. one threshold | eight calibrated channels fused by BP |
| DM’s role | confirm labels until the panel is complete | answer ≤ 3 content questions |
| Propagation | implicit (a name map) | explicit, measured, reported |
| Uncertainty | destroyed at the service boundary | first-class, gated, queryable |
| Blocking | `speaker_pending` blocks the pipeline | `attribution_review` is skippable |
| Wrong attribution in the wiki | caught only by the DM reading prose | structurally gated before generation |

# Session review — UX specification

Companion to `docs/attribution-model.md`. This document specifies the
experience the DM has after a session finishes transcribing, and how the
existing session page changes.

---

## 1. Principles

1. **The DM reviews *decisions*, not plumbing.** No `SPEAKER_XX`, no voice-cluster
   ids, no confidence matrices. The DM sees moments from their own session.
2. **Every question is answerable in seconds, from memory.** Each one shows the
   quoted moment and plays the audio on one click. A question that needs the DM
   to reason about the system is a generator bug.
3. **The system does the work in public.** After each answer, say what it
   unlocked. The DM must feel the leverage, or the review feels like chores.
4. **Nothing is compulsory.** *I don’t know*, **Skip**, and **Finish** exist at
   every step, and finishing early is a legitimate, non-punished outcome.
5. **Never ask the DM to fix text.** Fixing a name in the summary is a different
   job from telling the system who spoke; the two must not be conflated.
6. **Raw transcript is a reference, not a workspace.** Available, collapsed, and
   explicitly labelled as raw.

---

## 2. The session page, reordered

The current page renders the speaker panel and the full transcript prominently,
and the summary below them. The new order inverts that:

```
+--------------------------------------------------------------------------+
| <- back to campaign                                                      |
| The Sunless Citadel, part 3      #7   [review ready]                     |
| recorded 12 Jan - duration 3h 42m                                        |
+--------------------------------------------------------------------------+
|                                                                          |
|  SESSION REVIEW                                            94% resolved  |
|  We analysed your session and identified 94% of it automatically.         |
|  [############################------]  3 moments need your help.          |
|  Question 1 of 8.                           [ Answer the questions ]      |
|                                                                          |
+--------------------------------------------------------------------------+
|                                                                          |
|  SESSION SUMMARY            [ draft - awaiting review ]                   |
|  ...                                                                     |
+--------------------------------------------------------------------------+
|                                                                          |
|  PROPOSED WIKI CHANGES      [ computing... ]                             |
+--------------------------------------------------------------------------+
|                                                                          |
|  RECORDING / UPLOAD                                                      |
+--------------------------------------------------------------------------+
|                                                                          |
|  > Voices we found (4)                                     [ expand ]    |
|  > Moments we could not attribute (12)                     [ expand ]    |
|  > Raw transcript                                          [ expand ]    |
+--------------------------------------------------------------------------+
```

Everything the DM does not need by default is a collapsed disclosure. The raw
transcript moves from a 60vh scrolling panel in the middle of the page to the
last, collapsed item on it.

---

## 3. The review card

Three states: **nothing to review**, **review available**, **review complete**.

### 3.1 Review available

```
+--------------------------------------------------------------------------+
|  SESSION REVIEW                                            94% resolved   |
|                                                                          |
|  We analysed your session. We could identify most of it automatically -   |
|  three moments are still unclear.                                        |
|                                                                          |
|  We are confident about 94% of your session.       question 1 of 8       |
|  ################# ################# ##########  ###  #                  |
|  confirmed 41%      automatic 53%    uncertain 3%   ? 3%                 |
|  6% of what matters is still unattributed.                               |
|                                                                          |
|  [ Answer the questions ]   [ Finish anyway ]                            |
+--------------------------------------------------------------------------+
```

- The bar is **stakes-weighted speech time**, not a count of labels. Segments:
  *confirmed* (green) / *automatic* (blue) / *uncertain* (amber) / *unresolved*
  (grey). The legend is one line, always visible.
- **“question {n} of 8”** is where the DM is in a review of at most eight
  questions. There is no “questions to finish”: the greedy simulation behind that
  string is a best case (it said 6 on a session that then took 8 and ended with
  84 % unattributed), so the panel shows the two facts that hold instead (§9.4 of
  the model doc).
- **“Finish anyway”** is always present and never scolding: the summary and the
  wiki are generated from a 94%-resolved session just fine.

### 3.2 Nothing to review

```
|  SESSION REVIEW                                        100% resolved      |
|  We identified every moment of this session automatically.               |
```

Exactly the case the DM asked for: **no question at all** when the evidence is
decisive.

### 3.3 Review complete

```
|  SESSION REVIEW                                          97% resolved     |
|  Thanks - your 2 answers resolved 187 moments.                           |
|  14 moments remain unattributed and will be left out of wiki statements. |
+--------------------------------------------------------------------------+
```

---

## 4. The review flow

A focused overlay (a new `Modal` primitive — none exists today; `window.confirm`
is currently the only dialog) with exactly one question on screen.

### 4.1 A `who_did` question

```
+--------------------------------------------------------------+
|  Session review                               Question 1 of ~2|
|  [==================--------------------------------]         |
|                                                              |
|  Who cast Fireball on the three goblins?                     |
|                                                              |
|  "I cast fireball on the three goblins"                      |
|   00:41:15  -  three characters could have done this,        |
|               and the voice is unclear                        |
|  [ > Hear it ]                                               |
|                                                              |
|  [ Aramil - Player 1 (Wizard) ]                              |
|  [ Thorin - Player 2 (Fighter) ]                             |
|  [ Elara  - Player 4 (Cleric)  ]                             |
|  [ The Dungeon Master ]                                      |
|  [ Someone not in the campaign ]                             |
|                                                              |
|  [ I don't know ]                         [ Skip ]  [ Finish ]|
+--------------------------------------------------------------+
```

Notes on the anatomy:

- The **options are ordered by posterior**, most likely first, and each shows
  *character — player (class)*. The DM thinks in characters, so the character
  is the primary label; the player name disambiguates when two players are the
  same class.
- **Only the plausible candidates are listed** (posterior above a floor). If
  four characters were equally plausible they are all listed; if only two are,
  the DM sees two plus the two escape options. A shorter list is faster and the
  engine loses nothing, because the excluded members were already below the
  floor.
- **Hear it** plays the exact span through the existing `AudioPlayer` element
  (`seek(start)`, already implemented on the page). This is the single most
  valuable affordance in the flow and must be keyboard-accessible.
- The **why** line is short and honest: *“three characters could have done
  this, and the voice is unclear”*. It is the difference between a helpful
  system and an oracle.
- **Skip** and **Finish** are always visible. *I don’t know* is a real option,
  visually equal to the others, not a hidden fallback.

### 4.2 The questions are about moments, and nothing else

Both remaining kinds show one moment - the line, its span, a **Listen**, and the
roster in posterior order - and the card is the same card for both. What is
retired is everything that was not about a moment.

**The `presence` question was the last of them, and the DM's verdict killed it
in one sentence: *"usually in a scene 95 % of the characters are present"*.** That
is right, and the shape of the failure was worse than the wording:

- the answer was a foregone **yes** - on a real session the reading put the whole
  party in four stretches out of six - so the click bought the belief nothing;
- because it was priced at half a click and spanned a whole stretch, it took the
  ranking by storm: **20 of the 24 simulated candidates were presence questions**,
  and the questions that would have settled a moment were never even simulated.

So the review the DM saw was *"Was X there?"* asked six times about six people in
the same room, while the session's own summary said *"un personaggio si avvicina
allo sceriffo"* - the exact beat a moment question answers. The family is now in
`RETIRED_KINDS` with the voice kinds, and stored rows are dropped when a session
computed while it existed is reloaded.

What replaced it is not another kind. It is the same moment question, asked about
the moments that matter, plus one change on the summary side: the reading of who
was in a stretch now travels to the extractor (section 12.6.1 of the model doc),
so a solo stretch names the party member a narration is about instead of leaving a
hole the DM has to fill by hand.

### 4.3 The voice-identity questions are retired

`same_voice`, `different_voice`, `who_is_voice` and `new_person` are no longer
generated, and the ones stored on older sessions are dropped when the review
loads. They were measured against the DM's own ear and lost: the diarization
clusters they asked about are **mixtures of several people** (six of seven of
them contained clips of different voices), so *“who is this voice?”* had no true
answer, and a wrong answer was worse than no question — one click wrote a strong
prior onto every moment of a cluster that was not one person.

**The panel is not a question either.** *Where this session happens* (§3.4) shows
the stretches, their places, their casts and their exclusions — all visible, all
arguable — and it stays. What does not stay is asking the DM to *confirm the
cast*, one character at a time: the reading's cast list is context, and §4.2 above
is why.

### 4.4 After an answer

```
+--------------------------------------------------------------+
|  [check] Thanks. That also helped us identify 27 other        |
|          moments.                                            |
|                                                              |
|  Session is now 97% resolved.   [ Show me which ]             |
|                                                              |
|  [ Next question ]              [ Finish for now ]           |
+--------------------------------------------------------------+
```

**“Show me which”** opens a list of the newly resolved utterances with
timestamps and one-click seek. This is the trust-building moment: it is the
evidence that answers generalise, and it gives the DM a way to spot a bad
propagation immediately.

The number is real (`propagation_events.resolved_utterances`), never a
placeholder. If the answer resolved nothing beyond itself, the message is
*“Thanks — noted.”* and the system moves on.

### 4.5 When the DM answers *I don’t know*

```
+--------------------------------------------------------------+
|  No problem - we'll leave that one out rather than guess.     |
|  We won't ask about it again.                                |
|                                                              |
|  [ Next question ]              [ Finish for now ]           |
+--------------------------------------------------------------+
```

Two consecutive *I don’t know* answers end the review with:

```
|  That's fine - the remaining moments will simply be left      |
|  unattributed. Everything else is ready.                      |
+--------------------------------------------------------------+
```

No guilt, no repetition, and — importantly — **no fabricated label**.

---

## 5. Attribution in the summary

The summary card keeps its line-by-line review but gains a provenance chip per
line, which is the visible surface of the uncertainty model:

```
|  [Aramil]   cast Fireball on the three goblins, killing two.        |
|  [Thorin?]  charged the captain and was knocked prone.              |
|  [someone]  forced the eastern door.                                |
|  [the party] retreated to the courtyard and made camp.              |
```

| Chip | Meaning | Action on click |
|---|---|---|
| `Aramil` (neutral) | `user_confirmed` / `auto_high` / `propagated` | seek to the moment |
| `Thorin?` (amber) | `auto_low` | opens a one-line attribution picker for that moment |
| `someone` (grey) | `unresolved`, party-level wording | opens the same picker |
| `the party` | deliberately unattributed (aggregate action) | none |

This replaces “fix the transcript” with “tell me who this was”, which is the
same gesture the review flow uses — one picker component, reused.

The DM’s existing feedback loop (select lines, describe the change, regenerate)
is unchanged and remains the right tool for *content* corrections (“it was the
captain, not the goblin”) rather than identity corrections.

---

## 6. The demoted speaker panel: “Voices we found”

The current `speakers` card (label, badge, member `Select`, *Confirm*, *Confirm
all*) is replaced by a collapsed panel that reflects the new model:

```
+--------------------------------------------------------------------------+
|  Voices we found (4)                                          [ expand ]  |
+--------------------------------------------------------------------------+

  V1  Aramil        (Player 1)   32 min   confirmed                        |
  V2  Thorin        (Player 2)   41 min   confirmed                        |
  V3  Keth          (Player 3)    8 min   automatic                        |
  V4  unclear                     6 min   [ Who is this? ]   [ Split ]     |
                                                                           |
  Actions: [ Merge two voices ]  [ This voice is two people ]               |
+--------------------------------------------------------------------------+
```

- No raw diarization labels anywhere. `V1` is an internal handle, and even it is
  shown only here, never in the review flow.
- The primary row label is the resolved **character**, the secondary is the
  player, the third is speech time — which is the useful notion of “importance”.
- **Split** and **Merge** are explicit, first-class actions. They are the manual
  escape hatch for the exact pathology (one cluster, several people) that the
  old panel could not express at all.
- Status wording matches §7.1 of the model doc: *confirmed* / *automatic* /
  *uncertain* / *unresolved*.

---

## 7. The raw transcript, collapsed

```
> Raw transcript                                                           |
                                                                          |
  This is the raw diarization output. It may contain mistakes and is not    |
  used for the wiki - the review above is what the session is built from.   |
                                                                          |
  [ speaker chips = resolved names where known, "?" where not ]            |
  [ segment list, click to seek, max-h-[60vh] overflow-y-auto ]             |
                                                                          |
  [ download JSON ]                                                        |
```

The viewer stays **read-only by design**: attribution is never edited by typing on
a transcript line. Every correction goes through the same `AttributionPicker` the
review flow uses, so there is exactly one way to change a speaker and one place
where the evidence for it is recorded. The viewer's only interactions are seek and
opening that picker from a chip.

The existing `TranscriptViewer` is reused almost unchanged, with two edits:
its `speakerNames` map is fed from the attributed transcript instead of the
label map, and the low-confidence styling follows the new statuses rather than
the removed `LOW_SPEAKER_CONFIDENCE = 0.8` diarization threshold.

---

## 8. Edge states

| State | What the DM sees |
|---|---|
| Session still transcribing | The review card shows the pipeline stage; no review entry point |
| No usable audio | “We couldn’t analyse the voices in this recording, so the session is built from the text only.” plus the review card hidden and a lower coverage figure |
| One player at the table | Review card suppressed entirely (nothing to disambiguate) |
| Campaign with no voiceprints | Review card present, coverage low and honest; the questions come from the moments, and a one-line hint offers “Enrol player voices to cut this down next time” linking to the campaign members page |
| Developer account | Same view as the DM. Role gating is scattered in the current page (`isDm` alone for the speaker panel, `isDm \|\| isDeveloper` for the summary and change-set cards); the review follows the **summary/plan** convention, and `isDm`-only gating stays retired with the old panel. |
| Player (non-DM) viewing | No review card, no voices panel, collapsed transcript only; summary chips render names but are not clickable |
| Session already published | Review card becomes read-only history: “Reviewed on 14 Jan — 2 questions” |
| Everything unresolved | Coverage bar mostly grey, review offers the highest-stakes moments; the session is still confirmable |

---

## 9. Copy deck

Exact strings, written to be honest and non-blaming:

- Card title: **Session review**
- Ready: **We are confident about {pct}% of your session** — the stakes-weighted
  coverage of §11.1, worded as a state so that it is never read as a countdown to
  100 %.
- Progress: **question {n} of {max}** while there are questions left, and **all
  {max} questions answered** once the budget is spent. Two facts, no forecast.
  (There is deliberately **no** “about {k} questions to finish”: it was measured
  against reality — the plan said 6, the DM answered 8, the session ended with
  84 % unattributed — and no such number can be computed honestly. See §9.4 of
  the model.)
- Still left: **{pct} of what matters is still unattributed.**  A review can end
  with most of a session unresolved, and the panel must say so instead of
  announcing that there was nothing left to ask.
- Ending, by reason: “nothing left worth asking” for a belief that converged;
  “we have asked every question one review asks” for `budget`; “you said
  you did not know twice in a row” for `repeated_dont_know`; “review finished at
  your request” for the DM pressing Finish.
- The `budget` ending must not promise a later pass: reopening the review today
  means a recompute, which rebuilds the belief without the DM’s answers.
- After Finish the card is **removed from the page**: the review is closed, the
  session moves on to its summary, and the panel does not come back as “in
  review” on the next page load.
- Estimate ready: **Nothing to review — we recognised every moment.**
- Primary CTA: **Answer the questions**
- Secondary CTA: **Finish anyway**
- After an answer: **Thanks. That also helped us identify {n} other moments.**
- After a useless answer: **Thanks — noted.**
- I don’t know: **No problem — we’ll leave that one out rather than guess.**
- Two in a row: **That’s fine — the remaining moments will simply be left
  unattributed. Everything else is ready.**
- Panel title: **Voices we found**
- Unattributed block: **Moments we could not attribute ({n})**
- Raw transcript notice: **This is the raw diarization output. It may contain
  mistakes and is not used for the wiki.**

Everything is rendered in the **campaign language**, like the summary already
is (`session_summaries.language`).

---

## 10. What exists, what is new

Reused from `apps/web/components/ui.tsx`: `Card`, `Badge` (needs one new tone
`Button` / `buttonClass`, `Alert`, `EmptyState`, `Select`, `fmtDuration`,
`fmtPercent`, `Spinner` (currently exported and unused), plus
`TranscriptViewer`, `AudioPlayer` and the page’s existing `seek()` callback and
`objectUrl()` helper.

New components:

| Component | Notes |
|---|---|
| `Modal` | none exists today; `window.confirm` is the only dialog. Needed for the review overlay, keyboard-trapped and labelled |
| `SessionScenesCard` | **Where this session happens**: the stretches of the session as read from the transcript — place, moments, who is there, who is somewhere else, the event that started it — open by default, with no placeholder when a session has no reading. It exists because the engine EXCLUDES the members a stretch places elsewhere: an exclusion nobody can see is an exclusion nobody can correct, and the DM is the only one who knows whether the reading of their own table is right. It renders nothing at all when the session has no reading (an older session, or the engine off) |
| `CoverageBar` | stakes-weighted coverage of §11.1, plus the DM’s own progress
  through the questions (answered / max). It never claims a percentage it cannot
  support and never predicts a number of questions left: it shows the two facts
  that hold, and the residual “still unattributed” share under the bar. It is
  allowed to move DOWN when an answer contradicts earlier evidence — that is
  information, not a bug — but NOT because the engine re-labelled untouched
  moments as inferred (§10.0 of the model) |
| `SessionReviewCard` | the card in §3, wired to `GET /review` |
| `ReviewFlow` | the overlay in §4, one question at a time |
| `QuestionCard` | one shell for all three kinds: the prompt, the quote, the note, a listen button when the hook carries a span, and the options |
| `AttributionPicker` | the roster picker, reused by the flow, the summary chips and the voices panel |
| `ProvenanceChip` | the chip in §5 |
| `VoicesPanel` | §6, with `Split` / `Merge` actions |
| `UnattributedList` | §11.3 of the model doc |

The speaker UI currently lives inline in the 820-line
`apps/web/app/campaigns/[id]/sessions/[sessionId]/page.tsx`. The first
mechanical step of the frontend work is to extract it, because there is
currently no seam to replace it.

Polling: the review joins the existing `ACTIVE_STATUSES` poll and the
`settleTarget`/`sawWorking` pattern already used for the summary and the change
set. `attribution_review` is a resting status, so the pipeline poll is off there
and only the review endpoints are called.

### 10.1 Concrete blockers found in the current frontend

These are the specific things that must be cleared before the review can be
built, in the order they block:

1. **The speaker UI has no component boundary.** It is inline JSX inside the
   820-line `page.tsx` (roughly lines 697–816); `SessionSummaryCard` and
   `SessionPlanCard` are files, the speaker panel is not. Extract first.
2. **`Segment` and `TranscriptDoc` are not exported** from
   `components/session/transcript.tsx`, so no new component can consume them.
   Export them (and move them to the SDK, where the rest of the contract lives).
3. **There is no unassign path.** `SpeakerAssignment.status` can be `pending`,
   but no endpoint clears an assignment and nothing moves `auto -> pending`.
   The new `AttributionPicker` must support *unassign* / *not a party member*,
   or a DM who answers wrongly is stuck.
4. **`Confirm all` is a client-side loop** of N sequential POSTs with no bulk
   endpoint, no per-item progress and no rollback beyond `reloadSpeakers()`.
   It exists only for the escape hatch, and the new design needs a real
   `POST /voices/merge`-shaped operation rather than a loop.
5. **No modal, no progress bar, no toast, no tabs.** `window.confirm` is the
   only dialog in the app; `Spinner` is exported but unused everywhere.
6. **`SessionStatusTone` has no entries for the new statuses**;
   `SESSION_STATUS_TONE` in `components/ui.tsx` must gain `attributing`,
   `attribution_ready` and `attribution_review`.
7. **A hardcoded client threshold.** `LOW_SPEAKER_CONFIDENCE = 0.8` in
   `transcript.tsx` duplicates a server value that is already exposed and
   unused (`systemApi.speakerModel()` returns `{model, threshold, collection}`).
   Both go away: the chip styling follows the attribution status instead.
8. **`useAsyncData` has no mutation API**, so every mutation is
   *mutate -> reload*. The review flow wants local state between answers
   (the next question is already returned by the answer response), so it should
   hold its own state the way `SessionPlanCard` does, and treat the reload as
   reconciliation rather than the primary path.
9. **The diarization JSON is a full client-side download on every status
   change.** The review needs only the attribution list, which is a paginated
   API call; the transcript viewer can keep the full download behind the
   collapsed disclosure.
10. **The `<audio>` element and `seek(seconds)` live in the page.** Any new
    component in the flow must accept the same callback — that is what makes
    **Hear it** a one-line wiring job instead of new audio infrastructure.
11. **Display names cost N+1 `usersApi.get` calls** (one per distinct `user_id`)
    with a silent fallback to the raw uuid when the lookup fails. The review flow
    renders the roster on *every* question, so this becomes N+1 per question.
    The attributed transcript can carry `player_name` / `character_name` directly
    (content-service already resolves them today), which removes the lookups and
    the uuid fallback at once.

---

## 11. Mobile and the recording app

The Expo app keeps its single job (record, upload, show progress). One
addition, because it is the cheapest possible way to improve voice recognition:
after upload, the app offers **“Tag a voice”** for anyone at the table who has no
voiceprint — 10 seconds of speech, enrolled immediately. This is the existing
pre-naming flow reframed, and it removes the most expensive question kind
(`who_is_voice`) from the DM’s review entirely.

The review itself stays web-only: it needs the audio player, the roster and a
large target.
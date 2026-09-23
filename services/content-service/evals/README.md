# evals: session fixtures for the summariser

Real sessions, frozen, with the draft the pipeline actually produced from them and
the checks that draft must satisfy. The point is to make **"did that change make
summaries better or worse?"** a question with an answer.

It exists because it did not. The v15 summary work shipped blind: there is no
end-to-end quality test anywhere else in this service - every candidate in
`tests/` runs against a fake LLM - so a prompt or merger change could only be
judged by reading one session's output and forming an impression.

**Nothing under `app/` imports this package, and nothing here runs on its own.**
It is a dev tool with no effect on a deployed service.

## Running it

From `services/content-service`, on the host (needs the service's runtime deps,
notably `litellm`):

    python -m evals --list                 # what is installed, and how big
    python -m evals --baseline             # grade the stored draft: offline, free
    python -m evals bugie_inutili          # full run: extraction + compose + judge
    python -m evals                        # every installed fixture

Or with the service's real dependencies, without installing anything. Mount
`app/` as well as `evals/`: the image has a copy of the source baked in, so
without that mount the run silently exercises the code the image was built from
rather than the code in front of you — which is how a run "fails" with an
`unpack` error from a signature you already changed.

    docker compose run --rm --no-deps \
      -v "$PWD/services/content-service/app:/app/service/app" \
      -v "$PWD/services/content-service/evals:/app/service/evals" \
      content-service python -m evals

Exit status is 0 when every blocking check passes, 1 when one fails, 2 on a
fixture that cannot be loaded. An empty fixture set is **not** an error.

**`--baseline --judge` is the cheap loop.** It grades the stored draft and
nothing else — about ten judge calls, no extraction, no compose — so it answers
"do the semantic checks still fire?" in seconds. Use it after editing a
`criterion`; use a full run only when the pipeline itself changed.

## What a run does

A fixture carries ONE of the pipeline's two inputs, and the run takes the branch
the worker would take for it:

```
attributed.json (the redesign's input)
    -> _artifact_views -> extract_many -> merge_extractions -> apply_gate
    -> _compose_summary            (the SAME functions app.workers.generate calls)

transcript.json (DIARIZED: speaker labels, no attribution)
    -> _transcript_views         (+ the campaign roster, from fixture.yaml)
    -> extract_many -> merge_extractions
    -> exclude_character_names + rename_characters   (the roster's other half)
    -> _compose_summary            (no gate: there is no certainty data to gate on)

    -> grade, and write every intermediate artefact under evals/out/<fixture>/<run>/
```

A fixture may also declare the CAMPAIGN's `roster` (who plays whom, who is the
game master). The worker fetches that from campaign-service at run time; offline
the fixture carries it, in campaign-service's own member shape, so the run builds
its roster through the same `roster_from_members()` the worker uses. It is
campaign data, not session data - see a fixture's README for where its pairs come
from, what they fixed, and the two parts of it that were measured and REJECTED.

### The A/B pattern, and why every prompt change needs one

Settings that change what the model sees are readable from the environment, so a
change can be measured against its own absence without editing code:

    python -m evals --repeat 5 <fixture>                          # the change is on
    ROSTER_NOTE_ENABLED=false python -m evals --repeat 5 <fixture> # ... and off

Three results so far, all on S1E2 with five runs per arm:

| change | on | off |
|---|---|---|
| the roster's `[Table]` note | **2.2** contradictions | 7.4 |
| the character's physical description in that note | 5.6 | **2.2** |
| the cast reading's character names (app/speakers.py) | 16 | **1-4** |

The third row is the one to remember: handing a summariser a NAME for a voice, or
a way to PLACE one, reliably makes it place names it cannot support, and a wrong
name is applied to everything that follows. Context that says who EXISTS is worth
a great deal; context that claims who SPOKE is worth less than nothing.

### Every setting that changes the output, and what it measured

| setting | default | what it does, measured |
|---|---|---|
| `PROMPT_VERSION` | v26 | the prompt itself; the changelog in `app/prompts.py` carries the measurement behind each version |
| `ROSTER_NOTE_ENABLED` | true | the campaign roster's '[Table]' note: **2.2 contradictions against 7.4** over five runs an arm. Off keeps the roster's data (party line, player-name exclusion) and drops only its prompt |
| `CAST_READING_ENABLED` | true | the session's own narration read once; **narrators only** - carrying voice names measured 16 contradictions against 1-4 |
| `BEAT_LINES` | 25 | transcript lines per beat, the compression of the whole session. 25 -> 40/50 shortens the draft by ~15% and costs coverage AND accuracy (S1E1 facts missing 0.2 -> 2.5; S1E2 contradicted median 0 -> 5), measured twice |
| `SUMMARY_SENTENCES_TARGET` | 14 | the tightening pass over the finished record: sentences 16.7 -> 16.0 and 17.0 -> 15.3 with coverage held or better. 0 turns it off. It does NOT reach the published summary's ~10 sentences at any target |

That table is the honest summary of this harness: the summariser's remaining
distance from the published summaries is LENGTH, every lever on length has been
measured, and each one either fails or buys the shortness with facts.

Writing the artefacts is half the value: two runs can be **diffed** instead of
argued about. `views.json` is exactly what the extractor saw, `beats.txt` is what
the composer was given, `blocks.json` is what it produced.

## Benchmarks: is the draft as good as a human's?

Defect checks answer "is this known bug back". They cannot answer the question a
DM actually has, so a fixture may also carry what a PERSON wrote for the same
recording:

| file | what it is |
|---|---|
| `benchmark.txt` | the session summary the campaign wiki published - the TARGET |
| `truth.txt` | a longer human account of the same session - the ground truth for facts |

With a `benchmark.txt` present, every run is measured against it by
`evals/compare.py`, in the report and in `comparison.json`:

- **shape** - words, sentences, blocks. A 900-word draft against a 220-word
  benchmark is not a detail problem, it is a selection problem, and no model is
  needed to see it;
- **names** - the proper nouns the benchmark uses and the draft never mentions.
  A summary that never says who did what has not summarised the session;
- **content coverage** - the benchmark split into its own sentences, each one
  decided COVERED or NOT by one judge call. Per item, on purpose: an earlier
  version asked for "what is missing" as a free list and reported Miles Falco,
  the mirror, the indigo paintbrush and the explosion as missing on a draft that
  contained all four. A measurement that says a fact is absent when it is present
  is worse than no measurement;
- **contradictions** - the draft's statements the ground truth says are FALSE, and,
  reported separately, the statements the ground truth does not mention at all
  (`unsupported`). The split is not cosmetic: the ground truth is a summary too,
  and on a real run a draft that read "il paziente dice di chiamarsi Galgith, ma il
  gruppo conosce un altro Galgith" - both halves of a moment the recording really
  contains - was scored with 18 contradictions, nearly all of them items the human
  account simply skipped. Counting them together blames a draft for being faithful.
  What is left in `contradicted` after the split is a median of about two per run.

```
python -m evals --repeat 5         # run each fixture five times, then aggregate
python -m evals.history            # every run of every fixture, side by side
python -m evals.history <name>     # one fixture
python -m evals.regrade <name>     # re-measure stored runs with the CURRENT judge
python -m evals.voices <name>      # what the RECORDING says about its own voices
```

`voices` reads the transcript, not the draft: it collects every line where the
recording names its own speaker ("io sono Rendar", "Shiran: ...", "Dalia,
piacere.") and reports whether those statements agree with each other. When one
label speaks as three people and one person speaks under two labels, no
label-to-character map exists, and that is the measured reason the wrong character
lands on an action in the drafts - see a fixture's README. Run it before blaming a
prompt for an attribution error.

`regrade` exists because the instrument changes too: after the coverage judge was
rewritten, every comparison on disk had been produced by the old one, and grading
a new run against those numbers would have measured the judge rather than the
pipeline. It re-reads the stored narratives - no extraction, no compose, one
judge call per run.

**One run is a sample** (`llm_temperature` is 0.2): read the history table's
failure CLASSES across runs, never a single row.

## Checks

A check lives in a fixture's `expected.yaml`. Two kinds, on purpose:

- **`judge`** - asks a question the way a reader would ("does the narrative
  present the lunch-bringer as one person?"). It is the only kind that can catch a
  sentence stating a real fact about the wrong person, which is the failure mode
  that actually matters here. It is shown the transcript lines in `evidence`.
- **deterministic** - `min_beats`, `max_duplicate_similarity`,
  `labels_not_english`, `text_matches` / `text_not_matches`,
  `entity_names_not_matching` (the names the run would hand the wiki as PAGES,
  which the prose cannot show - "Uomo urlante", "Vice sceriffo"). Cheap,
  reproducible, cannot be talked out of a verdict, and blind to meaning. They are
  proxies: `max_duplicate_similarity` scores 0.45 on a real duplicate because the
  two calls that wrote it disagreed about the words, which is why a fixture can
  mark a check non-blocking.

`expected.yaml` also lists `baseline.known_failures`: the checks the **stored**
draft fails. That list is the eval's own self-test. It is graded offline on every
test run, and if one of those checks starts *passing*, the check has stopped
detecting the defect it was written for - a worse failure than a red run, so
`tests/test_evals_fixtures.py` errors on it.

## Adding a fixture

    python -m evals.capture --session <uuid> --name <name>

It reads the attributed transcript from MinIO and the shipped draft from
`dnd_content.session_summaries`, and writes `fixture.yaml`, `attributed.json`,
`baseline.json`, plus starter `expected.yaml` and `README.md` **only if those do
not already exist** - the checks are yours to write, and capture must not be able
to destroy them.

A session with no `attributed.json` (transcribed before the attribution engine
ran) and a session with no stored summary cannot be captured; both are refused
with an explanation rather than captured half-formed.

## Removing fixtures

**A fixture is a directory, and the directory is the whole fixture.** There is no
registry, no list to edit and no import to drop: `available()` scans the
filesystem, so a fixture exists exactly as long as its directory does.

    python -m evals --drop bugie_inutili --yes     # or just delete the directory

What survives, by design:

- **the harness keeps full coverage after the last fixture is gone.**
  `tests/test_evals_checks.py` and `tests/test_evals_fixtures.py` build their own
  synthetic fixture in `tmp_path`; the only test that needs a real recording skips
  itself when none is installed;
- `capture.py` stays, so a replacement can be made at any time;
- nothing outside `evals/` changes: no production code imports this package, no
  test depends on a fixture, and the only shared file touched is the `Makefile`
  (two targets). `.gitignore` did not need editing either - the existing `out/`
  rule already covers `evals/out/`.

## Known limitations

- **A run is a sample, and one sample will lie to you.** `llm_temperature` is
  0.2, so the same code produces a different set of defects each time. This is not
  a theoretical warning: the v16 partition was measured on one run that showed the
  seam defects fixed, and a second run of the same code put four of them back
  (see the fixture's README for both). It happened again while this harness was
  being built: the same prompt scored 1 contradiction on one run of a fixture and
  9 on the next. Compare failure **classes** across several runs and never report a
  single run as a result - which is why `--repeat N` exists:

      python -m evals --repeat 5                 # five runs per fixture + aggregate
      CAST_READING_ENABLED=false python -m evals --repeat 5    # the other arm

  The aggregate is the point: per-run rows, then the contradictions *by how many
  runs produced them*. A class present in every run is a property of the pipeline;
  a class present in one is a sample. Configure-by-environment is what makes an
  A/B possible without editing code (see `evals/README.md` and the settings
  comments for which flags are worth flipping).
- The judge is an LLM grading an LLM. It is shown the transcript lines the
  criterion is about, and it is asked to judge what the text says rather than
  what is plausible, but it is a reviewer, not an oracle.
- The deterministic checks read shape, not meaning. A run can pass all of them and
  still be wrong.
- A fixture is committed with real player names in it. That is a reason to delete
  a fixture, not a reason to edit it.

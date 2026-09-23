# Fixture: s1e2_bugie_inutili

A real session, with the summary a human wrote for it. See
[../../README.md](../../README.md) for the harness itself.

| | |
|---|---|
| session | `c3e927e2-7857-4aac-b112-c2ab353643d3` |
| campaign | InTale (Luxastra) |
| title | S1E2 - Bugie inutili |
| language | it |
| utterances | 646 |
| input | `transcript.json` - DIARIZED, speakers are `SPEAKER_00`.. |
| benchmark | `benchmark.txt` - the summary published on the campaign wiki |
| truth | `truth.txt` - a longer human account of the same session |

## What this fixture is for

The two older fixtures asserted DEFECTS: "this sentence names one person twice".
This one asserts a LEVEL. `benchmark.txt` is the summary a person wrote for the
same recording, and the question a run answers is how far the draft is from it -
in shape (words, sentences), in names (who the draft never mentions) and in
coverage (which events the draft leaves out). `evals/compare.py` prints all three
after every run.

The starting point is deliberately the DIARIZED transcript, not an attributed
one: this is what the pipeline has in hand before speaker identification has
named anybody. `speakers` in `fixture.yaml` is empty, so the view carries the raw
diarization labels, and the run measures what the summariser can do with them.

## Ground rules these fixtures were written under

- `benchmark.txt` and `truth.txt` are the WIKI's own words, copied unchanged.
  They are the target and the ground truth; neither is ever edited to fit the
  pipeline.
- a claim in the draft is judged against `truth.txt`, not against plausibility.

## What has been measured

The draft against `benchmark.txt`, one full run per row (`python -m evals.history`).
A single run is a sample - `llm_temperature` is 0.2 - so read the classes, not a row.

Across **24 runs** of every prompt version tried (v19 to v22), each row one full
run, measured per benchmark sentence:

| | words (bench 221) | names | benchmark sentences missing | contradicted | unsupported |
|---|---|---|---|---|---|
| range over all runs | 616-1016 | 75-100% | 0-2 of 9 | 0-6 | 3-30 |
| median | ~780 | ~95% | 1 | 2 | ~12 |

`contradicted` counts only statements the ground truth says are FALSE; a statement
the human account does not mention is `unsupported` and is reported apart, because
the human account is a summary too. Before that split, one run scored 18
"contradictions" that were almost entirely items the human summary skipped.

What the runs agree on, which is what a single row cannot say:

- **the facts get through.** Content coverage is 89-100%: each of the benchmark's
  own sentences, one verdict each, and the pipeline is not losing the session. An
  earlier report that listed Miles Falco, the mirror and the indigo paintbrush as
  missing was the MEASUREMENT being wrong, not the draft.
- **the draft is three to four times longer than the summary the wiki publishes**
  and carries more, not different, facts - the `unsupported` column is that extra
  detail. v21 asked for the published register and did not get it; on the other
  fixture the same instruction COST coverage (100% to 82%). Length follows the
  number of beats, not a sentence budget.
- **handing the extraction a NAME for a voice is a large regression.** The first
  '[Cast]' note said "SPEAKER_01 = Shiran, use this name for what that voice says":
  name coverage fell to 75%, one run merged Sir Lucius into Hann ("Hann entra
  nell'ospedale e trova Hann"), and the draft rewrote the session around a wrong
  identity. A name is applied to every beat that voice speaks in; a narrator
  assignment that is wrong costs only its own lines. The note now carries narrators
  only.
- **one voice, two labels.** The recording's diarization is not reliable enough to
  name voices from: inside a single scene one person speaks under two labels, and a
  line a player opens with their own character's name ("Shiran: ...") is labelled
  differently a few minutes later.


## The roster in `fixture.yaml`, and where it comes from

`roster:` is CAMPAIGN data, not session data: who plays whom, and who is the game
master. The deployed worker fetches it from campaign-service
(`GET /internal/campaigns/{id}/members` - the endpoint refiner-service already
uses for speaker attribution), so it is an input production has and an offline run
must be given. The transcript's own `refiner.cast` block confirms this campaign
has 7 members with descriptions.

These six pairs were reconstructed from the wiki's own account of the table and
from the session's own introductions ("Dalia, piacere.", "Shiran, cin!",
"Shiran: non appena sento la parola indaco..."). They say who PLAYS whom; they say
nothing about who did what in the session, which is what the draft is measured on.

What the roster fixed, measured over 3 runs each:

| | before v23 | with the roster |
|---|---|---|
| `no-player-names` (S1E1) | FAIL on every run ("Giulia si avvicina alle guardie") | **PASS on every run** |
| characters drafted (S1E1) | included Giulia and Tommy, both tagged `is_party` | players gone; the six PCs are exactly the six `is_party` characters |
| words (S1E2, bench 221) | 692-1016 | 607-675 |
| names (S1E1) | 56-69% | 69-75% |

## Two things about the roster that were measured, not assumed

**Showing the pairs to the extraction is worth a lot.** Five runs with the
`[Table]` note against five without it (nothing else changed):

| S1E2, 5 runs each | contradictions vs the ground truth | words |
|---|---|---|
| note **on** | 0, 3, 1, 2, 5 - **mean 2.2** | ~703 |
| note **off** | 4, 10, 5, 12, 6 - **mean 7.4** | ~726 |

Without it the draft invents its own cast ("una scimmietta parlante che dice di
essere la coscienza di Rendar"), and the failures are all in the same direction.

**The character's physical description is NOT shown, and that is a measurement
too.** campaign-service keeps one (`character_description`, the field
refiner-service feeds to its own LLM pass), and rendering it - "Galgith (played by
Gianandrea) - fanciulla bionda" - was tried to let the summariser place a name from
a physical detail. Five runs against the same five without it:

| S1E2, 5 runs each | contradictions | name coverage | words |
|---|---|---|---|
| note with appearance | 4, 7, 11, 3, 3 - **mean 5.6** | **85%** | ~631 |
| note without | 0, 3, 1, 2, 5 - **mean 2.2** | 95% | ~703 |

It is the same lesson as the cast reading that carried voice names
(`app/speakers.py`): material that lets a model PLACE a name makes it place names
it cannot support - "Rendar punta un pugnale al fianco di Letho", "Galgith gli
prende un polso", "il mezzorco dottore lo porta nel suo studio" - and a wrong name
is applied to everything that follows. The fixture still carries the field; the
note never renders it.

A last sentence spelling that lesson out in the note itself ("the roster says who
EXISTS, not who spoke") was tried and is gone: over the runs it was in, S1E2's mean
sat at 4.75 against 2.2 for the note it was added to. That is inside the session's
noise, which is the point - **every sentence in that note should be one that was
measured**, and this one was not.

### The measurement's own floor

S1E2's contradicted count swings from 0 to 12 across runs of IDENTICAL code
(50 runs, mean 3.4, median 3). Any A/B whose arms differ by fewer than about four
contradictions cannot be resolved with five runs an arm, and a three-run sample
cannot resolve anything: the same configuration measured 1.3 on one three-run
sample and 5.3 on the next. Treat every number in this file as a direction, and
raise `--repeat` before believing a small one.


## The entity entries: what a run would hand the wiki

The prose can read perfectly while the run drafts pages nobody wants. Before v24,
every run of both fixtures produced character entries called `Sceriffo`,
`Vice sceriffo`, `Vicario`, `Creatura piumata`, `Uomo urlante`, `la nana`,
`la scimmietta`, and on S1E2 the two FACTIONS the session is about, `Elfi` and
`Indaco` - eight to nine pages per session that the DM would have to delete.

Two changes, both measured over four runs:

| | before | with v24 |
|---|---|---|
| `no-role-or-description-characters` | **FAIL on every run** (1-4 junk entries) | **PASS in 7 of 8 runs** |
| characters drafted (S1E1) | 12, including "Uomo urlante", "Sceriffo" | the six PCs + Sir Lucius, Antonikus, Malagrad |
| contradictions | mean ~3.4 (S1E2, 7 runs) | **mean 1.5** (S1E1) / **1.5** (S1E2), 4 runs each |

The prompt rule (a character is ONE PERSON with a name the table uses; a role, a
description and a faction are none of those) took the entries from nine to one to
four - and no further. What finished it is code (`merger.is_generic_name`): a
phrase whose head is a generic noun and which contains no CAPITALISED word after
the first is a description, not a name. Checking one end of the phrase is not
enough, because Italian puts the head first ("creatura piumata") and English puts
it last ("screaming man"); a capitalised word inside the phrase is what separates
"Sir Lucius" and "Vice Sceriffo Miles Falco" from the phrases above.

What is left is a different class: `Hyman` and `Enzo`, two NAMES the extraction
invented for the patient the recording never names (one is a player's surname, the
other a mis-hearing). They are proper-looking, so no shape rule catches them, and
the draft also uses them in the prose ("chiede di Hyman, l'uomo che urlava").



## The length gap, and what has been tried on it

The draft is two to four times the length of the published summary, and the extra
length is extra CONTENT, not padding: the food, the waiters, the dice rolls, the
furniture, the monkeys. The published summary carries the turning moments and
nothing else.

| change | what it did |
|---|---|
| v21: ask for 10-16 sentences | did not shorten it, and COST coverage (100% to 82%). A budget says how much to cut, not what |
| v25: name what to KEEP and what to DROP | obeyed: sentences 20.0 -> 15.2 (S1E1) and 24.7 -> 19.8 (S1E2) over four runs each. Words and accuracy stayed inside the noise |
| `BEAT_LINES=50`: half as many beats | works, and is NOT free - see below |

The composer will REPACK content when asked and will not DROP it, so the length
follows the material it is handed. `BEAT_LINES` is that lever, and four runs an
arm on both fixtures (beat_lines 25 -> 50) say what it costs:

| | S1E1 | S1E2 |
|---|---|---|
| beats | 23.5 -> 21.3 | 33 -> 25 |
| words | 522 -> 485 | 798 -> 648 |
| sentences | 17.5 -> 14.0 | 25.2 -> 17.8 |
| benchmark sentences missing | 0.8 -> 0.3 | **0.8 -> 1.5** |
| contradicted | 3.3 -> 3.0 | **4.5 -> 7.8** |

A quarter fewer beats buys a fifth shorter draft and costs coverage and accuracy on
the session whose facts are dense - the same lesson the v15/v16 notes in
`evals/README.md` already recorded. The default stays at 25: **the length of this
draft is the price of the facts in it**, and a DM who wants the published
summary's compactness can lower the setting knowingly.

**A dead parameter hid this for one round.** `beat_budget()` took a
`lines_per_beat` argument and kept dividing by the module constant, so the first
A/B measured 21 beats against 22 and looked like "the model ignores the budget".
It was the pipeline ignoring the setting; `tests/test_chunking.py::
test_the_compression_setting_actually_changes_the_budget` now fails if that
happens again.


## Why the wrong character appears on an action, and why it cannot be fixed here

Every attempt to fix that class inside the summariser failed until v26, and
`python -m evals.voices` shows why. A label has to mean one person before a
summariser can be told which character a voice is. On this recording it does not:

```
- [00:08:57] SPEAKER_02 --self_intro--> Rendar
- [00:09:01] SPEAKER_05 --self_intro--> Letho
- [00:13:03] SPEAKER_00 --self_intro--> Rendar
- [00:13:22] SPEAKER_01 --pleasure--> Shiran
- [00:13:27] SPEAKER_01 --self_intro--> Dalia
- [00:22:01] SPEAKER_00 --self_intro--> Miles
- [00:48:56] SPEAKER_04 --self_intro--> Galgith

SPEAKER_00 speaks as: Miles, Rendar
SPEAKER_01 speaks as: Dalia, Shiran
Rendar speaks under: SPEAKER_00, SPEAKER_02
```

One label covers three people, one person speaks under two labels. There is no
function from label to character, so there is nothing correct to tell the
summariser - which is exactly what was measured: handing it a mapping made drafts
worse (16 contradictions against 1-4, `app/speakers.py`).

**What DID work is telling it not to guess from the cast.** The failure was not
borrowing a name from elsewhere, it was abductive: the scuffle is spoken by
`SPEAKER_00` and `SPEAKER_03`, while Rendar (`SPEAKER_02`) and Letho
(`SPEAKER_05`) are the two characters the model CAN name in that scene - so it
paired the two names it had with the two people in the fight. v26 says a scene's
cast is not evidence of who acted in it, and the class fell from **7 runs in 10 to
1 in 5**, with the median contradictions going from 3 to 0. An earlier attempt at
a neighbouring rule (v24, "do not lend a name to another voice") changed nothing:
5 runs in 6.

The remaining structural fix is upstream and the platform has it: refiner-service's
speaker mode re-clusters the voices onto CONSISTENT canonical labels, and
speaker-service names them. This transcript was captured with that pass off
(`refiner.speakers_refined: false` in the file itself).

## The speaker map this fixture does NOT have

`speakers` in `fixture.yaml` is empty, so the view carries `SPEAKER_00`..`06`
and each chunk has to infer, on its own, who is who. **That is not the state the
deployed pipeline runs in**: by the time `content.generate` is published,
speaker-service has matched voiceprints and the DM has named every remaining
voice, so the view reads `[00:14:40] Galgith: ...` and the summariser never has
to guess.

The identity errors measured here are therefore a FLOOR, not a measurement of the
summariser. Fill `speakers:` in with the mapping the campaign actually has
(label -> character) and re-run: that run measures the summariser on the input it
is designed for, and the difference between the two is what the missing speaker
map costs.

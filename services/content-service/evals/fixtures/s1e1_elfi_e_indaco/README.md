# Fixture: s1e1_elfi_e_indaco

A real session, with the summary a human wrote for it. See
[../../README.md](../../README.md) for the harness itself.

| | |
|---|---|
| session | `54e9ec84-1caa-4621-8130-045b195576f7` |
| campaign | InTale (Luxastra) |
| title | S1E1 - Elfi e indaco |
| language | it |
| utterances | 391 |
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

Each row is one whole run, measured per benchmark sentence by `evals/compare.py`.

| | words (bench 273) | sentences (bench 11) | names | facts missing | contradicted |
|---|---|---|---|---|---|
| v20-v22, 15 runs | 400-644 | 13-25 | 56-69% | 0-2 of 11 | 0-8, median 2 |
| **v25+v26, 4 runs** | **578** | **17.2** | **70%** | **0.2 of 11** | **0, 0, 0, 0** |

The last four runs are the first time this session's draft has been clean: **every
statement the human account disagrees with is gone**, with coverage held (10.8 of
11 benchmark facts) and one run in the set landing at 12 sentences against the
benchmark's 11. The class that went away is the one v26 targeted - see *Why the
wrong character appears on an action* below.

`contradicted` counts only statements the ground truth says are FALSE - the words
the human account does not use at all are `unsupported`, reported apart, because a
summary that skips a moment has not said the draft got it wrong.

This fixture is the harder of the two on names: 56-69% of the benchmark's proper
nouns appear in the draft, because the benchmark's own hook is the pair of words
shouted in the street ("Elfi", "Indaco") and the draft tells the scene without
them.

The failure CLASSES this session reproduces across runs - which is what makes it
worth keeping, since a single run is a sample:

- the blonde woman's description (the stolen body, the transferred soul) is given
  to the wrong party member;
- a player's REAL name reaches the narrative ("Giulia si avvicina alle guardie"),
  because nothing in a diarized view says which names are players and which are
  characters;
- a small figure is described as staring at Hann "con sguardo da predatore" - a
  detail no line supports.


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
| `no-player-names` | FAIL on every run ("Giulia si avvicina alle guardie") | **PASS on every run** |
| characters drafted | included Giulia and Tommy, both tagged `is_party` | players gone; the six PCs are exactly the six `is_party` characters |
| names | 56-69% | 69-75% |

Two things about the roster were measured on the other fixture and are recorded
there in full: showing the pairs to the extraction is worth about **2.2 vs 7.4**
contradictions over five runs against five, and showing each character's PHYSICAL
description - though campaign-service keeps one and the refiner uses it - made the
draft clearly worse (5.6 vs 2.2, name coverage 95% to 85%), so it is carried and
not rendered.


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

`speakers` in `fixture.yaml` is empty, so each chunk infers who is who from the
raw `SPEAKER_xx` labels, independently of the others. The deployed pipeline does
not run that way: speaker identification has already happened by the time
content-service is asked for a summary. Filling `speakers:` in (label ->
character) runs the other branch and measures the summariser on the input it is
designed for.

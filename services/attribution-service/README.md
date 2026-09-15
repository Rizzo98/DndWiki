# attribution-service

The evidence-first speaker attribution engine (see
`docs/attribution-model.md`, `docs/adr/0002-evidence-first-speaker-attribution.md`).

It answers one question per utterance - *who said this?* - and reports how sure
it is, instead of mapping each diarization label to one person and calling that
a fact.

## What it owns

- `dnd_attribution` (PostgreSQL): utterances, voice identities, the evidence
  log, the current belief, the review, campaign-level learning.
- `voice_observations` / `member_voice_models` are **read**, not written:
  speaker-service owns the vectors in Qdrant. This service owns the inference.

## Layers

```
  1  audio segment      (transcription)
  2  diarization label  EVIDENCE, never identity
  3  voice identity     anonymous, session-scoped, has a purity estimate
  4  member             the posterior over candidates
  5  character          what the wiki is allowed to say
```

A diarization label is never treated as a person: one label may hold several
people (the DM voicing NPCs) and one person may span several labels.

## Layout

| Module | Role |
|---|---|
| `app/utterances.py` | diarization segments -> utterances (pure) |
| `app/clustering.py` | vectorised UPGMA, pinned against `dnd_common.clustering` |
| `app/structure.py` | split / merge detection and resolution |
| `app/channels.py` | the evidence channels -> log likelihood ratios (pure) |
| `app/inference.py` | damped loopy belief propagation, mean-field fallback |
| `app/calibration.py` | cosine -> log LR, channel weights, per campaign |
| `app/questions.py` | the six question kinds + suppression rules |
| `app/ranking.py` | expected global entropy reduction, greedy selection |
| `app/propagate.py` | one answer -> the whole session |
| `app/review.py` | the review loop and the stopping criterion |
| `app/evidence.py` | the identity-evidence LLM pass |
| `app/capabilities.py` | who can do what |

## Running

```sh
alembic upgrade head
python -m app.workers.compute     # consumes attribution.jobs
uvicorn app.main:app --port 8000  # review + internal APIs
```

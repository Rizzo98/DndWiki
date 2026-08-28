# refiner-service

LLM contextual diarization: the stage between transcription and speaker
identification that produces the **finalized raw transcript**.

Raw ASR + diarization is imperfect: the text has transcription errors and the
speaker labels are unreliable (they can be swapped between people and reset at
every chunk boundary). This service asks an LLM to fix both **using context**:

- **transcription errors** — spelling, names, grammar, punctuation. Fidelity
  to the exact words is explicitly secondary: the goal is a transcript that
  tells one coherent, consistent story.
- **diarization errors** — the LLM reassigns speaker labels so every distinct
  speaker keeps ONE label for the whole session, which is impossible for a
  per-chunk diarizer to do on its own.

The output is a "finalized raw" transcript + diarization (canonical
`SPEAKER_XX` labels, corrected text, **timings unchanged**) that
speaker-service then matches against the enrolled voiceprints.

## Pipeline position

```
recorded -> transcribing -> transcribed -> refining -> refined
    -> identifying_speakers -> speakers_identified | speaker_pending -> ...
```

- Consumes `transcripts.refine` (routing key `transcription.completed`).
- Moves the session `transcribed -> refining -> refined` via the
  session-service internal API.
- Rewrites `transcripts/<session>/transcript.json` and
  `diarization.json` **in place** (same URIs the UI polls), adding a
  top-level `refiner` object `{enabled, provider, model, prompt_version}`.
- Publishes `transcription.refined` with the refined segments.

When `REFINER_ENABLED=false` the worker acks and does nothing: speaker-service
identifies directly from `transcription.completed` (with its own embedding
re-clustering), exactly as before this service existed.

## How the LLM pass works

1. Segments are grouped into **speaker turns** (shared
   `dnd_common.transcript.speaker_turns`: contiguous same-raw-label runs,
   bounded by silence gaps and chunk boundaries).
2. Turns are sent to the LLM (LiteLLM, JSON schema) in **sliding windows**
   (`REFINER_WINDOW_TURNS`, `REFINER_WINDOW_OVERLAP`). Each window returns
   one object per turn: corrected `speaker` + `text`.
3. The window overlap turns are already-finalized context — the LLM keeps
   their labels, which is how speaker identity carries across windows. A
   compact per-label "rolodex" of already-finalized speakers is included so a
   speaker absent from the overlap still reuses their label.
4. Decisions are canonicalized (`SPEAKER_XX`, first-appearance stable) and
   mapped back onto the original segments: `start`/`end`/`chunk` are
   preserved, only `speaker` and `text` change. The corrected turn text is
   re-split across the turn's segments proportionally to their original
   lengths, so the transcript view and audio seek keep working.

Voiceprint matching is untouched: speaker-service matches the refined labels
against the campaign voiceprints (or the DM names the pending ones, which
enrolls the voice).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `REFINER_ENABLED` | `true` | run the LLM pass (false = pass-through no-op) |
| `LLM_PROVIDER` | `deepseek` | provider recorded on the artifacts |
| `LLM_MODEL` | `deepseek/deepseek-chat` | LiteLLM model string (routes the API) |
| `REFINER_MODEL` | *(empty)* | per-stage override; empty = `LLM_MODEL` |
| `REFINER_TEMPERATURE` | `0.0` | editing should be deterministic |
| `REFINER_MAX_TOKENS` | `4096` | per-window output cap |
| `REFINER_JSON_RETRIES` | `1` | corrective retries on malformed JSON |
| `REFINER_PROMPT_VERSION` | `v1` | prompt/schema version recorded on artifacts |
| `REFINER_WINDOW_TURNS` | `100` | turns per LLM call |
| `REFINER_WINDOW_OVERLAP` | `15` | leading already-finalized turns per non-first window |
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / ... | | provider keys (same as content-service) |

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/refiner-service
python -m app.workers.refine
uvicorn app.main:app --reload --port 8010
```

The LLM is imported lazily; unit tests fake every I/O boundary (storage,
session-service, LLM) and run without litellm installed.

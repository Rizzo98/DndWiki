# transcription-openai-service

API-backed transcription worker — a **drop-in replacement** for the on-prem
WhisperX `transcription-service`. It consumes the same `transcription.jobs`
queue, drives the same session state machine through session-service's
internal API, writes the same MinIO artifacts
(`transcripts/<session>/transcript.json` + `diarization.json`) and publishes
the same `transcription.completed` event — but instead of loading WhisperX +
pyannote + wav2vec2 locally (multi-GB models, GPU recommended), it makes a
single HTTPS call to OpenAI's Speech-to-Text API with the
[`gpt-4o-transcribe-diarize`](https://developers.openai.com/api/docs/guides/speech-to-text)
model, which performs ASR **and** speaker diarization in one request.

## Why this variant

| | on-prem (`docker-compose.yml`) | API (`docker-compose.api.yml`) |
|---|---|---|
| ASR | WhisperX (`small`/`large-v3`) | OpenAI `gpt-4o-transcribe-diarize` (cloud) |
| Diarization | pyannote/speaker-diarization-3.1 (gated, HF token) | built into the model (`diarized_json`) |
| Word alignment | wav2vec2 per-language checkpoints | n/a (no word-level timings) |
| Local ML runtime | torch + models (multi-GB, GPU recommended) | **none** — thin HTTP worker |
| Cost | free (your hardware/electricity) | per audio token (~$2.50 / 1M input audio tokens at time of writing) |
| Network | none required | must reach `api.openai.com` |

Speaker **identification** is untouched: after `transcription.completed`,
the existing `speaker-service` slices each diarized label out of the
recording, embeds it with ECAPA-TDNN (lightweight, CPU-friendly) and matches
it against the campaign voiceprints in Qdrant. The voiceprint naming system
(profile enrollment via user-service, session-derived enrollment when the DM
names a speaker) is fully preserved.

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | *(empty)* | **Required.** Placeholder in compose; fill before use. |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-transcribe-diarize` | ASR + diarization model. |
| `OPENAI_RESPONSE_FORMAT` | `diarized_json` | Must stay `diarized_json` to receive speaker labels. |
| `OPENAI_CHUNKING_STRATEGY` | `auto` | Server VAD chunking; required for audio > 30 s. |
| `OPENAI_TRANSCRIPTION_LANGUAGE` | *(empty)* | Optional ISO-639-1 hint (empty = server auto-detect). |
| `OPENAI_CHUNK_SECONDS` | `300` | Chunk length for the local split (same default as the on-prem `WHISPER_CHUNK_SECONDS`). |
| `OPENAI_MAX_UPLOAD_MB` | `24` | Safety cap per uploaded WAV chunk — keep below OpenAI's 25 MB hard limit. |
| `OPENAI_API_BASE` | `https://api.openai.com/v1` | Override for Azure OpenAI / LiteLLM proxy / compatible gateways. |
| `OPENAI_REQUEST_TIMEOUT_SEC` | `900` | Per-request timeout (long sessions can take a while). |
| `OPENAI_MAX_RETRIES` | `3` | Client-side retries for 429/5xx/network errors. |

## Chunked uploads (25 MB API limit)

OpenAI's transcription endpoint caps each upload at **25 MB**, which is far
too small for a full session recording. This worker therefore mirrors the
on-prem worker's chunking:

1. **Decode** the recording locally with ffmpeg to 16 kHz mono PCM WAV (no
   ML — a plain codec/container conversion; the same representation WhisperX
   uses).
2. **Split** it into fixed-length chunks (`OPENAI_CHUNK_SECONDS`, default
   300 s ≈ 9.2 MiB per chunk at 16 kHz mono).
3. **Transcribe** each chunk with one API call (`gpt-4o-transcribe-diarize`,
   `diarized_json`); the server's `chunking_strategy=auto` further chunks
   long inputs internally if needed.
4. **Offset + merge**: each chunk's diarized segments are shifted by the
   chunk's start time and appended in sequence, producing one continuous
   session timeline.

After every chunk the partial artifacts are overwritten in MinIO (same URIs
the UI polls) and a `transcription.progress` event is published — results
become visible before the session completes, exactly like the on-prem worker.

> Note on speaker labels: diarization runs per chunk, so the same real
> speaker may receive different labels in different chunks (e.g. `Speaker 1`
> in chunk 1 vs `Speaker 2` in chunk 2). This is the same behavior as the
> on-prem chunked WhisperX worker (labels are per-chunk there too) and is
> resolved downstream: `speaker-service` embeds every label and matches it
> against the campaign voiceprints, so all labels of one voice resolve to
> the same person.

## Differences vs the on-prem worker

- **No word-level timings.** `transcript.json` segments carry `words: []`.
  The UI and the LLM pipeline treat words as optional (click-to-seek uses
  segment `start`, which is present).
- **No local ML models.** The container only needs ffmpeg for the decode
  step; no torch/whisperx/pyannote, no GPU, no model downloads, no `models`
  volume.
- **Egress.** The audio is sent to the API (per chunk), so the machine must
  have internet egress; a rejected chunk (bad key, oversized file) fails the
  session with a clear error.

## Failure semantics

Identical to the on-prem worker: transient failures re-raise (the broker
retries with backoff), pipeline failures mark the session `failed` and
re-raise (the redelivered copy is acked as a no-op via `ConflictTransition`),
and cancellation never leaves a session stuck in `transcribing`.

## Tests

```bash
cd services/transcription-openai-service
python -m pytest
```

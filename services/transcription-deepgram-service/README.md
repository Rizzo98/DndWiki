# transcription-deepgram-service

API-backed transcription worker — a **drop-in replacement** for the on-prem
WhisperX `transcription-service` and a sibling of the OpenAI variant. It
consumes the same `transcription.jobs` queue, drives the same session state
machine through session-service's internal API, writes the same MinIO
artifacts (`transcripts/<session>/transcript.json` + `diarization.json`)
and publishes the same `transcription.completed` event — but instead of
loading WhisperX + pyannote + wav2vec2 locally (multi-GB models, GPU
recommended), it uploads each audio chunk to
[Deepgram's Speech-to-Text API](https://developers.deepgram.com/docs/stt/getting-started)
with the
[Nova 3](https://developers.deepgram.com/docs/models-languages-overview)
model, which performs ASR **and** speaker diarization in one request.

## Why this variant

| | on-prem (`docker-compose.yml`) | Deepgram (`docker-compose.transcription.yml`) |
|---|---|---|
| ASR | WhisperX (`small`/`large-v3`) | Deepgram Nova 3 (cloud) |
| Diarization | pyannote/speaker-diarization-3.1 (gated, HF token) | built into the model (`diarize=true`) |
| Word alignment | wav2vec2 per-language checkpoints | Deepgram returns word timings out of the box |
| Local ML runtime | torch + models (multi-GB, GPU recommended) | **none** — thin HTTP worker |
| Cost | free (your hardware/electricity) | per audio minute (pay-as-you-go) |
| Network | none required | must reach `api.deepgram.com` |

Speaker **identification** is untouched: after `transcription.completed`,
the existing `refiner-service` (optional LLM contextual pass) and
`speaker-service` match the diarized labels to the campaign voiceprints in
Qdrant (ECAPA-TDNN embeddings). The voiceprint naming system (profile
enrollment via user-service, session-derived enrollment when the DM names a
speaker) is fully preserved.

## Enabling the variant

Deepgram and OpenAI are two interchangeable API backends behind **one**
compose override. Pick the provider with a single variable:

```bash
# Deepgram (Nova 3)
TRANSCRIPTION_PROVIDER=deepgram docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build

# OpenAI (gpt-4o-transcribe-diarize)
TRANSCRIPTION_PROVIDER=openai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
```

Set `DEEPGRAM_API_KEY` (Deepgram) or `OPENAI_API_KEY` (OpenAI) in `.env`
first. The GPU override (`docker-compose.gpu.yml`) and the API overrides are
mutually exclusive — use one or the other.

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `DEEPGRAM_API_KEY` | *(empty)* | **Required.** Fill before processing jobs. |
| `DEEPGRAM_TRANSCRIPTION_MODEL` | `nova-3` | Nova 3 = Deepgram's most accurate model (ASR + diarization). |
| `DEEPGRAM_LANGUAGE` | *(empty)* | BCP-47 hint (`it`, `en`, `it-IT`, ...). Empty = `language=multi`: Nova auto-detects per chunk (required — Deepgram's default when omitted is **English**, not auto-detect). |
| `DEEPGRAM_SMART_FORMAT` | `true` | Normalize numbers/dates/currency. |
| `DEEPGRAM_PUNCTUATE` | `true` | Add punctuation. |
| `DEEPGRAM_CHUNK_SECONDS` | `300` | Chunk length for the local split (same default as the on-prem `WHISPER_CHUNK_SECONDS`). Deepgram accepts larger payloads; raise it if you prefer fewer calls. |
| `DEEPGRAM_MAX_UPLOAD_MB` | `24` | Safety cap per uploaded WAV chunk. |
| `DEEPGRAM_API_BASE` | `https://api.deepgram.com/v1` | Override for a gateway/proxy. |
| `DEEPGRAM_REQUEST_TIMEOUT_SEC` | `900` | Per-request timeout. |
| `DEEPGRAM_MAX_RETRIES` | `3` | Client-side retries for 429/5xx/network errors. |

## Chunked uploads

The recording is processed the same way the on-prem worker (and the OpenAI
variant) does:

1. **Decode** the recording locally with ffmpeg to 16 kHz mono PCM WAV (no
   ML — a plain codec/container conversion; the same representation WhisperX
   uses).
2. **Split** it into fixed-length chunks (`DEEPGRAM_CHUNK_SECONDS`, default
   300 s ≈ 9.2 MiB per chunk at 16 kHz mono) — bounded payloads keep
   per-request latency low and side-step Deepgram's plan-dependent
   per-request size limits.
3. **Transcribe** each chunk with one call to
   `POST /v1/listen?model=nova-3&diarize=true&utterances=true&smart_format=true&punctuate=true`.
   `utterances=true` returns speaker turns (start/end/transcript/words/
   speaker), which map 1:1 to the pipeline's segments; the parser falls back
   to paragraphs, then to consecutive same-speaker word runs, if utterances
   are ever absent.
4. **Offset + merge**: each chunk's diarized segments are shifted by the
   chunk's start time and appended in sequence, producing one continuous
   session timeline.

After every chunk the partial artifacts are overwritten in MinIO (same URIs
the UI polls) and a `transcription.progress` event is published — results
become visible before the session completes, exactly like the on-prem worker.

> Note on speaker labels: Deepgram numbers speakers per request (0, 1, 2, ...).
> They are normalized to the canonical `SPEAKER_XX` format the pipeline
> expects. Diarization runs per chunk, so the same real speaker may receive a
> different number in different chunks — the same behavior as the on-prem
> chunked worker, resolved downstream: speaker-service embeds every label and
> matches it against the campaign voiceprints, so all labels of one voice
> resolve to the same person.

## Differences vs the on-prem worker

- **Cloud ASR + diarization.** The container only needs ffmpeg for the decode
  step; no torch/whisperx/pyannote, no GPU, no model downloads, no `models`
  volume.
- **Word-level timings.** Unlike the OpenAI variant, Deepgram returns word
  timestamps, so `transcript.json` segments carry real `words` lists —
  matching the on-prem worker's shape (click-to-seek works on both segment
  and word level).
- **Diarization confidence.** Every segment carries `speaker_confidence`
  (0..1, direct field or the mean of its words). The web UI highlights
  labels whose confidence is below the low-confidence threshold so the DM can
  re-check and re-assign them. The refiner and speaker-service preserve the
  field through LLM refinement and voiceprint re-clustering.
- **Egress.** The audio is sent to the API (per chunk), so the machine must
  have internet egress; a rejected chunk (bad key) fails the session with a
  clear error.

## Failure semantics

Identical to the on-prem worker: transient failures re-raise (the broker
retries with backoff), pipeline failures mark the session `failed` and
re-raise (the redelivered copy is acked as a no-op via `ConflictTransition`),
and cancellation never leaves a session stuck in `transcribing`.

## Tests

```bash
cd services/transcription-deepgram-service
python -m pytest
```

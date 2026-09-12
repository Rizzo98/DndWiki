# transcription-assemblyai-service

API-backed transcription worker — a **drop-in replacement** for the on-prem
WhisperX transcription-service and a sibling of the Deepgram/OpenAI
variants. It consumes the same 'transcription.jobs' queue, drives the same
session state machine through session-service's internal API, writes the
same MinIO artifacts ('transcripts/<session>/transcript.json' +
'diarization.json') and publishes the same 'transcription.completed' event
— but instead of loading WhisperX + pyannote + wav2vec2 locally (multi-GB
models, GPU recommended), it uploads each audio chunk to AssemblyAI and
runs a
[Universal-3.5 Pro](https://www.assemblyai.com/docs/pre-recorded-audio/select-the-speech-model)
transcript job, which performs ASR **and** speaker diarization in one
request.

## Why this variant

| | on-prem (docker-compose.yml) | AssemblyAI (docker-compose.transcription.yml) |
|---|---|---|
| ASR | WhisperX (small/large-v3) | AssemblyAI Universal-3.5 Pro (cloud) |
| Diarization | pyannote/speaker-diarization-3.1 (gated, HF token) | built into the model (speaker_labels=true) |
| Word alignment | wav2vec2 per-language checkpoints | AssemblyAI returns word timings out of the box |
| Local ML runtime | torch + models (multi-GB, GPU recommended) | **none** — thin HTTP worker |
| Cost | free (your hardware/electricity) | per audio minute (pay-as-you-go) |
| Network | none required | must reach api.assemblyai.com |

Speaker **identification** is untouched: after 'transcription.completed',
the existing refiner-service (optional LLM contextual pass) and
speaker-service match the diarized labels to the campaign voiceprints in
Qdrant (ECAPA-TDNN embeddings). The voiceprint naming system (profile
enrollment via user-service, session-derived enrollment when the DM names a
speaker) is fully preserved.

## Enabling the variant

AssemblyAI, Deepgram and OpenAI are interchangeable API backends behind
**one** compose override. Pick the provider with a single variable:

```bash
# AssemblyAI (Universal-3.5 Pro)
TRANSCRIPTION_PROVIDER=assemblyai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build

# Deepgram (Nova 3) / OpenAI (gpt-4o-transcribe-diarize)
TRANSCRIPTION_PROVIDER=deepgram docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
TRANSCRIPTION_PROVIDER=openai   docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build
```

Set 'ASSEMBLYAI_API_KEY' in .env first (get one at
https://www.assemblyai.com/dashboard). The GPU override
(docker-compose.gpu.yml) and the API overrides are mutually exclusive —
use one or the other.

## Per-campaign parameters (from the campaign config)

Unlike the other backends (whose language hint is a plain env var), this
worker reads the campaign configuration from campaign-service at job time:

| AssemblyAI parameter | Source | Notes |
|---|---|---|
| language_code | campaign.language | The language the DM chose at campaign creation (it, en, de, fr, es, pt — all supported by AssemblyAI). Falls back to ASSEMBLYAI_LANGUAGE, then auto-detection. |
| speakers_expected | number of campaign members | The people at the table (DM + players) — a hard boundary on diarization labels. |
| prompt | campaign name/description + built-in prompt | Contextual prompting ([docs](https://www.assemblyai.com/docs/pre-recorded-audio/universal-3-5-pro/prompting)): plain-language description of what the audio is (a D&D session), enriched with the campaign name and description. Override with ASSEMBLYAI_PROMPT. |
| keyterms_prompt | roster names (expanded) | Character names — full name AND each single word of multi-word names (the short forms players use at the table) — so fantasy names are transcribed accurately. Player names used when a member has no character. Disable with ASSEMBLYAI_KEYTERMS_ENABLED=false. |

The campaign fetch is **best-effort**: if campaign-service is unreachable
or the campaign is gone, the job still runs with AssemblyAI auto-detection,
no speaker hint and the built-in prompt — exactly the same degradation
policy as the refiner's cast fetch.

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| ASSEMBLYAI_API_KEY | *(empty)* | **Required.** Fill before processing jobs. |
| ASSEMBLYAI_TRANSCRIPTION_MODEL | universal-3-5-pro | ASR + diarization model. |
| ASSEMBLYAI_LANGUAGE | *(empty)* | Optional override; empty = campaign language, then auto-detect. |
| ASSEMBLYAI_PROMPT | *(built-in)* | Contextual prompt override; empty = built-in D&D-session prompt + campaign context. |
| ASSEMBLYAI_KEYTERMS_ENABLED | true | Inject roster names (full + single words) as keyterms_prompt. |
| ASSEMBLYAI_CHUNK_SECONDS | 300 | Chunk length for the local split (same default as the other workers). |
| ASSEMBLYAI_MAX_UPLOAD_MB | 24 | Safety cap per uploaded WAV chunk. |
| ASSEMBLYAI_API_BASE | https://api.assemblyai.com | Override for a gateway/proxy. |
| ASSEMBLYAI_REQUEST_TIMEOUT_SEC | 900 | Per-request timeout. |
| ASSEMBLYAI_MAX_RETRIES | 3 | Client-side retries for 429/5xx/network errors. |
| ASSEMBLYAI_POLL_INTERVAL_SEC | 3 | Poll interval for the async transcript job. |
| ASSEMBLYAI_POLL_TIMEOUT_SEC | 1800 | Max wait for one transcript job. |
| CAMPAIGN_SERVICE_URL | http://campaign-service:8000 | Where the campaign language/roster are read from. |

## Chunked uploads (async jobs)

AssemblyAI's pre-recorded API is asynchronous and needs a URL it can fetch,
so each chunk goes through three steps:

1. **Decode** the recording locally with ffmpeg to 16 kHz mono PCM WAV (no
   ML — a plain codec/container conversion; the same representation
   WhisperX uses).
2. **Split** it into fixed-length chunks (ASSEMBLYAI_CHUNK_SECONDS,
   default 300 s ≈ 9.2 MiB per chunk at 16 kHz mono).
3. **Upload + submit + poll**: upload the chunk WAV to 'POST /v2/upload'
   (raw bytes), submit a 'POST /v2/transcript' job (speech_models:
   ['universal-3-5-pro'] — model selection is an ARRAY in the current API,
   speaker_labels=true, plus the per-campaign parameters above) and poll
   'GET /v2/transcript/{id}' until completed (or error).
4. **Offset + merge**: each chunk's diarized utterances (milliseconds →
   seconds, speaker letters A/B/C → canonical SPEAKER_XX) are shifted by
   the chunk's start time and appended in sequence, producing one
   continuous session timeline.

After every chunk the partial artifacts are overwritten in MinIO (same URIs
the UI polls) and a 'transcription.progress' event is published — results
become visible before the session completes, exactly like the on-prem worker.

> Note on speaker labels: AssemblyAI labels speakers per job (A, B, C, ...)
> normalized to SPEAKER_XX. Diarization runs per chunk, so the same real
> speaker may receive a different label in different chunks — the same
> behavior as the on-prem chunked worker, resolved downstream:
> speaker-service embeds every label and matches it against the campaign
> voiceprints, so all labels of one voice resolve to the same person.
> speakers_expected (the member count) is a hard boundary per chunk: the
> model never returns more labels than that. If the count is ever wrong
> (e.g. a guest at the table), prefer speaker_options min/max — see the
> [diarization docs](https://www.assemblyai.com/docs/pre-recorded-audio/label-speakers).

## Differences vs the on-prem worker

- **Cloud ASR + diarization.** The container only needs ffmpeg for the
  decode step; no torch/whisperx/pyannote, no GPU, no model downloads, no
  models volume.
- **Word-level timings.** Like Deepgram, AssemblyAI returns word
  timestamps, so transcript.json segments carry real words lists —
  matching the on-prem worker's shape (click-to-seek works on both segment
  and word level).
- **Per-segment confidence.** AssemblyAI reports a text-transcription
  confidence (0..1) per utterance; it is surfaced as
  speaker_confidence so the web UI's low-confidence flagging keeps
  working (it is a text signal, not a diarization one). The same value
  is also exposed under the segment's plain `confidence` key, which
  refiner-service reads to decide whether the context can raise a
  low-confidence sentence (other backends omit the key entirely).
- **Egress.** The audio is sent to the API (per chunk upload), so the
  machine must have internet egress; a rejected chunk (bad key) fails the
  session with a clear error.

## Failure semantics

Identical to the on-prem worker: transient failures re-raise (the broker
retries with backoff), pipeline failures mark the session failed and
re-raise (the redelivered copy is acked as a no-op via
ConflictTransition), and cancellation never leaves a session stuck in
transcribing.

## Tests

```bash
cd services/transcription-assemblyai-service
python -m pytest
```

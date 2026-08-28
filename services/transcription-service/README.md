# transcription-service

WhisperX worker: consumes `transcription.jobs` and produces word-level
transcripts with speaker diarization.

## Pipeline (per job)

1. Update `sessions.status = transcribing` via the session-service internal API.
2. Download raw audio from MinIO (`recordings/<session_id>/raw.<ext>`).
3. Decode the recording and split it into `WHISPER_CHUNK_SECONDS` (default 5
   minute) chunks.
4. Process each chunk, one at a time: ASR (`whisperx.load_model(WHISPER_MODEL)`
   — `small` default, CPU-friendly; loaded once per process and kept warm) →
   VAD → word-level alignment (wav2vec2) → diarization
   (`pyannote/speaker-diarization-3.1`, gated — set `HF_TOKEN`) → assign
   diarized speakers to segments.
5. After every chunk, overwrite `transcripts/<session_id>/transcript.json`
   (full, with word timings) + `diarization.json` (compact segments) in MinIO,
   keep the session artifact pointers fresh, and publish
   `transcription.progress` with the cumulative segments — results become
   visible before the session finishes.
6. After the last chunk, fix the authoritative duration and move
   `sessions.status = transcribed` (internal API).
7. Publish `transcription.completed` (with the compact segment list for
   speaker-service).

On failure the session is marked `failed` with the error recorded; the worker
acknowledges redeliveries of a failed/duplicate job as no-ops (the state
machine rejects the transition), so retries are safe and idempotent.
Cancellation (e.g. the broker closing the channel mid-job) also marks the
session `failed` instead of leaving it stuck in `transcribing`.

## Long jobs, the broker consumer timeout and cancellation

RabbitMQ >= 3.13 defaults to a 30-minute `consumer_timeout`: a consumer that
does not ack within that window has its channel closed (unacked messages are
requeued). Recordings are processed in `WHISPER_CHUNK_SECONDS` chunks, so
each unit of work stays small (a 5-minute chunk transcribes in minutes on
CPU) — the job queues' `x-consumer-timeout` = 2 h (see
`infrastructure/rabbitmq/definitions.json` and `dnd_common.events`) is a
generous ceiling rather than a live requirement. If a job is nevertheless
cancelled mid-pipeline:

- the session is marked `failed` (never stuck in `transcribing`), and the
  redelivered message is acked as a no-op;
- the pipeline runs in a worker thread (`asyncio.to_thread`), and Python
  cannot kill threads: the orphaned WhisperX thread keeps consuming CPU/RAM
  until it finishes on its own, and the loaded models stay cached in the
  process afterwards by design. For prompt, hard resource release on
  cancellation, run the pipeline in a subprocess instead of a thread.

Startup: the worker connects to RabbitMQ via `dnd_common.events.connect_rabbitmq`
(`fail_fast=False`), so a worker that boots before the broker is ready keeps
retrying instead of crashing — a startup race that previously killed the
worker process on every full-stack restart (the container stayed up because
uvicorn survived, but the consumer was gone).

## Configuration

| Env | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `small` | any whisperx-supported size; `large-v3` needs a GPU |
| `DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` | requires HF_TOKEN |
| `WHISPER_BATCH_SIZE` | 8 | per-GPU batch |
| `WHISPER_CHUNK_SECONDS` | 300 | split the recording into fixed-length chunks; partial results are published as `transcription.progress` after every chunk |
| `WHISPER_COMPUTE_TYPE` | `float16` | GPU; on CPU falls back to `WHISPER_CPU_COMPUTE_TYPE` |
| `WHISPER_CPU_COMPUTE_TYPE` | `int8` | CTranslate2 CPU compute (memory-safe; float32 OOMs) |
| `WHISPER_DEVICE` | auto | `cuda` / `cpu` / empty = auto-detect |
| `PREWARM_LANGUAGES` | — | comma-separated ISO codes whose alignment checkpoints are pre-downloaded at container start (e.g. `it,en`) |
| `HF_TOKEN` | — | required for gated pyannote models |
| `SESSION_SERVICE_URL` | `http://localhost:8003` | internal worker API |
| `WORK_DIR` | `/tmp/dnd-transcription` | staging dir for raw audio |

## Model downloads

- WhisperX ASR + pyannote diarization models are cached under `HF_HOME`
  (`/app/models/hub`, on the `models_cache` volume).
- The per-language wav2vec2 alignment checkpoints (torchaudio VoxPopuli
  bundles) are cached under `TORCH_HOME` (`/app/models/torch`, also on the
  volume) — so they survive container recreates instead of re-downloading
  on every startup.
- `PREWARM_LANGUAGES` (comma-separated ISO codes, e.g. `it,en`) pre-downloads
  the alignment checkpoint(s) at container start, before the worker consumes
  its first job. Empty = no prewarm (the checkpoint is still fetched lazily
  on first use, but only once thanks to the volume cache).

## CPU-only

The worker runs fully on CPU out of the box — no NVIDIA hardware required:

- Device auto-detection: CUDA when available, otherwise `cpu`.
- `float16` automatically falls back to `int8` on CPU — the ASR backend is
  faster-whisper/CTranslate2, where int8 is the memory-safe choice (large-v3
  float32 needs ~6 GB of RAM and gets OOM-killed on typical dev VMs).
- The default Docker image (`python:3.12-slim`) installs CPU torch wheels;
  the GPU override (`.gpu.yml`) is the only path that pulls CUDA builds.
- Force the device explicitly with `WHISPER_DEVICE=cpu` (handy on GPU hosts
  or in CI).

CPU is a correctness-first fallback, not a performance target: `large-v3` on
CPU transcribes slower than real-time by a large margin. The default is
`WHISPER_MODEL=small`, which is the sensible CPU size; `medium` is a middle
ground, and `large-v3` should only be used on GPU. Consider lowering
`WHISPER_BATCH_SIZE` if memory is tight.

## GPU

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d transcription-service
```

The GPU override swaps the base image to `pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime`
and reserves one NVIDIA device. CPU fallback works (slow) with `float32`.

## Local dev

```bash
pip install -e libs/python/dnd_common
pip install -e services/transcription-service
python -m app.workers.transcribe          # worker
uvicorn app.main:app --reload --port 8004 # API
```

Tests (no GPU/whisperx needed — the pipeline is mocked):

```bash
cd services/transcription-service && pytest
```

## TODO

- [ ] VAD preprocessing for long recordings (2h+)
- [ ] Progress reporting (sessions.status updates mid-run)
- [ ] Pre-warm models at startup (optional cold-start optimization)

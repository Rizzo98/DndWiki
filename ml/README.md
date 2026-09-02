# ml/ — models & assets

Notes on the ML components. **Models are never committed** — they are
downloaded at runtime into the `models_cache` volume (`/app/models`) and can be
pre-seeded for faster cold starts.

## Components

| Component | Model | Where | Notes |
|---|---|---|---|
| ASR + diarization (on-prem) | [WhisperX](https://github.com/m-bain/whisperx) — `small` (CPU-friendly default) + `pyannote/speaker-diarization-3.1` | transcription-service | GPU recommended; `float16` |
| ASR + diarization (API variant, Deepgram) | [Deepgram Nova 3](https://developers.deepgram.com/docs/models-languages-overview) (`diarize=true`) | transcription-deepgram-service | Cloud; no local models — see [its README](../services/transcription-deepgram-service/README.md); enable with `docker-compose.transcription.yml` (`TRANSCRIPTION_PROVIDER=deepgram`, the default) |
| ASR + diarization (API variant, OpenAI) | OpenAI `gpt-4o-transcribe-diarize` (`diarized_json`) | transcription-openai-service | Cloud; no local models — see [its README](../services/transcription-openai-service/README.md); enable with `TRANSCRIPTION_PROVIDER=openai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d` (or the legacy `docker-compose.api.yml`) |
| Speaker embeddings | [SpeechBrain](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) `spkrec-ecapa-voxceleb` (ECAPA-TDNN, 192-d) | speaker-service / user-service | CPU-friendly |
| LLM | provider-swappable via [LiteLLM](https://github.com/BerriAI/litellm) | content-service | `deepseek/*` (default), `openai/*`, `anthropic/*`, `ollama/*`, `vllm/*` |

## Credentials & licensing

- **pyannote models are gated on Hugging Face.** Accept the license at
  https://huggingface.co/pyannote/speaker-diarization-3.1 and set `HF_TOKEN`
  in `.env`, otherwise diarization fails at runtime.
- Whisper (`MIT`) and SpeechBrain (`Apache-2.0`) are open.

## Pre-seeding (optional, faster cold start)

```bash
docker compose up -d transcription-service   # first run downloads models
# or pre-warm manually:
docker run --rm -v dnd-wiki_models_cache:/app/models \
  -e HF_TOKEN=$HF_TOKEN python:3.12-slim \
  sh -c "pip install -q huggingface_hub && huggingface-cli download pyannote/speaker-diarization-3.1 --local-dir /app/models"
```

## Environment toggle

- GPU: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`
- CPU fallback: the worker auto-falls back to `int8` (faster-whisper/CTranslate2),
  which is memory-safe; consider `WHISPER_MODEL=small` for acceptable throughput.
- API (no local ML): `docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d`
  — replaces WhisperX + pyannote with a cloud backend selected by
  `TRANSCRIPTION_PROVIDER`: Deepgram Nova 3 (default, `diarize=true`) or
  OpenAI `gpt-4o-transcribe-diarize`. Speaker identification (ECAPA-TDNN
  embeddings vs Qdrant voiceprints) still runs locally and is unchanged, so
  the voiceprint naming system is fully preserved.

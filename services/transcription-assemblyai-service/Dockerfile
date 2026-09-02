# transcription-assemblyai-service
#
# Thin API worker: NO local ML models at all. Consumes transcription.jobs,
# uploads each audio chunk to AssemblyAI (POST /v2/upload), submits a
# Universal-3.5 Pro transcript job with speaker diarization and polls it to
# completion; stores the same artifacts the on-prem WhisperX worker would
# produce. No GPU, no torch, no whisperx, no pyannote, no model downloads —
# the image is a plain HTTP worker.
#
# Wire it up with docker-compose.transcription.yml:
#   TRANSCRIPTION_PROVIDER=assemblyai docker compose -f docker-compose.yml -f docker-compose.transcription.yml up -d --build

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# ffmpeg: decode the recording to 16 kHz mono PCM WAV before chunking —
# a plain codec/container conversion, no ML involved.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/transcription-assemblyai-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common \
 && pip install --no-cache-dir -e /app/service

WORKDIR /app/service

EXPOSE 8000
# Run the queue worker AND the small API (health/model status) in one container.
CMD ["sh", "-c", "python -m app.workers.transcribe & exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

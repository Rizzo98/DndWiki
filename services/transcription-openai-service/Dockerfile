# transcription-openai-service
#
# Thin API worker: NO local ML models at all. Consumes transcription.jobs,
# uploads the recording to OpenAI /audio/transcriptions
# (gpt-4o-transcribe-diarize -> diarized_json) and stores the same artifacts
# the on-prem WhisperX worker would produce. No GPU, no torch, no whisperx,
# no pyannote, no model downloads — the image is a plain HTTP worker.
#
# Wire it up with docker-compose.api.yml:
#   docker compose -f docker-compose.yml -f docker-compose.api.yml up -d --build

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# ffmpeg: only needed to losslessly remux the rare raw .aac/.opus uploads
# into OpenAI-supported containers (m4a/ogg) — no codecs/models involved.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/transcription-openai-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common \
 && pip install --no-cache-dir -e /app/service

WORKDIR /app/service

EXPOSE 8000
# Run the queue worker AND the small API (health/model status) in one container.
CMD ["sh", "-c", "python -m app.workers.transcribe & exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

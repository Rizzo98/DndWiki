# transcription-service
#
# Runs WhisperX (ASR + diarization + word alignment) as a queue worker.
# CPU-only by default: the plain python image pulls CPU torch wheels from PyPI
# (torch >= 2.6 ships CPU-only wheels on Linux), and the worker auto-detects
# the device with an int8 (CTranslate2) fallback — no NVIDIA hardware required.
# Use docker-compose.gpu.yml to switch the base image to PyTorch/CUDA (which
# already bundles CUDA torch, satisfying the pyproject constraint) and reserve
# a GPU.

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# ffmpeg + build tools (needed for audio decoding and some pip wheels)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg build-essential curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/transcription-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common \
 && pip install --no-cache-dir -e /app/service

WORKDIR /app/service

# WhisperX models are cached on a shared volume (models_cache):
# - HF_HOME: huggingface hub cache (ASR + diarization)
# - TORCH_HOME: torch.hub cache (wav2vec2 alignment checkpoints)
ENV HF_HOME=/app/models
ENV TORCH_HOME=/app/models/torch

EXPOSE 8000
# Pre-download the alignment checkpoints (PREWARM_LANGUAGES), then run the
# worker AND the small API (health/model status) in one container.
CMD ["sh", "-c", "python -m app.prewarm; python -m app.workers.transcribe & exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

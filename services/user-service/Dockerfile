# user-service
FROM python:3.12-slim

# torchaudio decodes m4a/aac/webm via ffmpeg; build-essential for torch wheels
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared library first, then this service (build context = repo root)
COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/user-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common \
 && pip install --no-cache-dir -e "/app/service[ml]"

WORKDIR /app/service
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# speaker-service
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/speaker-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common \
 && pip install --no-cache-dir -e /app/service

WORKDIR /app/service

ENV HF_HOME=/app/models

EXPOSE 8000
CMD ["sh", "-c", "python -m app.workers.identify & exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

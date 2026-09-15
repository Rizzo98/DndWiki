# attribution-service
FROM python:3.12-slim

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/attribution-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common && pip install --no-cache-dir -e /app/service

WORKDIR /app/service
EXPOSE 8000
# Migrations run before the API comes up (idempotent)
CMD ["sh", "-c", "alembic upgrade head && (python -m app.workers.compute &) && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

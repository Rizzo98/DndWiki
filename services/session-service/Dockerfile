# session-service
FROM python:3.12-slim

WORKDIR /app

COPY libs/python/dnd_common /app/libs/dnd_common
COPY services/session-service /app/service

RUN pip install --no-cache-dir -e /app/libs/dnd_common  && pip install --no-cache-dir -e /app/service

WORKDIR /app/service
EXPOSE 8000
# Postgres is waited for first (dnd_common.wait): a database that is still
# replaying WAL answers TCP and refuses queries with 'the database system is
# starting up' (57P03). That is transient - dying on it is not.
# Migrations run before the API comes up (idempotent)
CMD ["sh", "-c", "python -m dnd_common.wait && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

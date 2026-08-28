"""content-service — FastAPI entrypoint (health, job status, debug regenerate).

Runs in the same container as the queue worker; exposes lightweight status
endpoints plus the DM-only debug regenerate trigger. The heavy LLM pipeline
lives in app.workers.generate.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dnd_common.db import get_session
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import regenerate
from app.broker import EventPublisher
from app.core.config import get_settings
from app.services import jobs, summaries

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the event publisher; RabbitMQ connect failure is non-fatal."""
    publisher = EventPublisher(settings.rabbitmq_url)
    try:
        await publisher.connect()
    except Exception:  # noqa: BLE001 — broker may be starting; publish() retries lazily
        logger.warning("RabbitMQ not reachable at startup; will retry on first publish")
    app.state.publisher = publisher
    try:
        yield
    finally:
        await publisher.close()


app = FastAPI(title="content-service", version="0.1.0", lifespan=lifespan)
app.include_router(regenerate.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.service_name}


@app.get("/api/content/jobs/{session_id}")
async def generation_job(session_id: str, db: AsyncSession = Depends(get_session)):
    """Newest generation job for a session (or null when none ran yet)."""
    from uuid import UUID

    try:
        job_id = UUID(session_id)
    except ValueError:
        return {"session_id": session_id, "job": None}
    job = await jobs.latest_job_for_session(db, job_id)
    if job is None:
        return {"session_id": session_id, "job": None}
    return {
        "session_id": session_id,
        "job": {
            "id": str(job.id),
            "status": job.status,
            "llm_provider": job.llm_provider,
            "llm_model": job.llm_model,
            "prompt_version": job.prompt_version,
            "draft_ids": job.draft_ids or [],
            "confidence": float(job.confidence) if job.confidence is not None else None,
            "error": job.error,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        },
    }


@app.get("/api/content/summaries/{session_id}")
async def session_summary(session_id: str, db: AsyncSession = Depends(get_session)):
    """Merged LLM summary for a session (or null when generation never ran).

    Same contract as generation_job: an invalid id is not an error, the body
    just carries a null summary so the UI can treat it as "not generated yet".
    """
    from uuid import UUID

    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return {"session_id": session_id, "summary": None}
    row = await summaries.latest_summary_for_session(db, session_uuid)
    if row is None:
        return {"session_id": session_id, "summary": None}
    return {
        "session_id": session_id,
        "summary": {
            "id": str(row.id),
            "session_id": str(row.session_id),
            "generation_job_id": str(row.generation_job_id) if row.generation_job_id else None,
            "summary": row.summary,
            "characters": row.characters or [],
            "locations": row.locations or [],
            "events": row.events or [],
            "timeline_entries": row.timeline_entries or [],
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "llm_provider": row.llm_provider,
            "llm_model": row.llm_model,
            "prompt_version": row.prompt_version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
    }


@app.get("/api/content/llm")
async def llm_config():
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        # The prompt version this deployment currently uses; the UI shows it so
        # the label tracks prompt evolution instead of the version stored on
        # the last generation job/summary.
        "prompt_version": settings.prompt_version,
    }

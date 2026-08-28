"""generation_jobs persistence + status helpers (worker and API)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationJob

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


async def create_job(
    db: AsyncSession,
    session_id: UUID,
    *,
    provider: str,
    model: str,
    prompt_version: str,
) -> GenerationJob:
    """Record a generation run as started (status='running')."""
    job = GenerationJob(
        session_id=session_id,
        status=RUNNING,
        llm_provider=provider,
        llm_model=model,
        prompt_version=prompt_version,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def complete_job(
    db: AsyncSession,
    job_id: UUID,
    *,
    draft_ids: list[UUID | str],
    confidence: float,
) -> GenerationJob:
    """Mark a run done with the created draft page ids + overall confidence."""
    job = await db.get(GenerationJob, job_id)
    if job is None:
        raise ValueError(f"generation job {job_id} not found")
    job.status = DONE
    job.draft_ids = [str(d) for d in draft_ids]
    job.confidence = confidence
    job.error = None
    await db.commit()
    await db.refresh(job)
    return job


async def fail_job(db: AsyncSession, job_id: UUID, error: str) -> GenerationJob:
    """Record a failed run (error text kept for the DM)."""
    job = await db.get(GenerationJob, job_id)
    if job is None:
        raise ValueError(f"generation job {job_id} not found")
    job.status = FAILED
    job.error = error[:2000]
    await db.commit()
    await db.refresh(job)
    return job


async def latest_job_for_session(db: AsyncSession, session_id: UUID) -> GenerationJob | None:
    """Newest generation run for a session (job status endpoint)."""
    stmt = (
        select(GenerationJob)
        .where(GenerationJob.session_id == session_id)
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
        .limit(1)
    )
    return await db.scalar(stmt)

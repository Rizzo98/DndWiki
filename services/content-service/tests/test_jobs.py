"""Tests for the generation_jobs service layer."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app import services as job_services
from app.models import GenerationJob


async def test_create_complete_fail_lifecycle(session_factory):
    async with session_factory() as db:
        job = await job_services.create_job(
            db,
            UUID("11111111-1111-1111-1111-111111111111"),
            provider="litellm",
            model="openai/gpt-4o-mini",
            prompt_version="v1",
        )
        assert job.status == "running"
        assert job.llm_provider == "litellm"
        assert job.draft_ids == []

        job = await job_services.complete_job(
            db,
            job.id,
            draft_ids=["a", "b"],
            confidence=0.75,
        )
        assert job.status == "done"
        assert job.draft_ids == ["a", "b"]
        assert float(job.confidence) == 0.75

        job = await job_services.fail_job(db, job.id, "something went wrong")
        assert job.status == "failed"
        assert "went wrong" in job.error


async def test_latest_job_for_session_returns_newest(session_factory):
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    async with session_factory() as db:
        first = await job_services.create_job(
            db, session_id, provider="litellm", model="m1", prompt_version="v1"
        )
        await job_services.fail_job(db, first.id, "first failed")
        # backdate the first run: SQLite CURRENT_TIMESTAMP has 1s resolution,
        # so two jobs in the same second would otherwise tie on created_at
        first.created_at = datetime.now(UTC) - timedelta(seconds=5)
        await db.commit()
        second = await job_services.create_job(
            db, session_id, provider="litellm", model="m2", prompt_version="v1"
        )

        latest = await job_services.latest_job_for_session(db, session_id)
        assert latest is not None
        assert latest.id == second.id
        assert latest.status == "running"

        assert await job_services.latest_job_for_session(
            db, UUID("99999999-9999-9999-9999-999999999999")
        ) is None


async def test_complete_job_unknown_raises(session_factory):
    async with session_factory() as db:
        import pytest

        with pytest.raises(ValueError, match="not found"):
            await job_services.complete_job(
                db, UUID("00000000-0000-0000-0000-000000000000"),
                draft_ids=[], confidence=0.5,
            )


async def test_generation_job_row_shape(session_factory):
    """Column defaults match docs/data-model.md (draft_ids default [])."""
    async with session_factory() as db:
        job = await job_services.create_job(
            db,
            UUID("11111111-1111-1111-1111-111111111111"),
            provider="litellm",
            model="openai/gpt-4o-mini",
            prompt_version="v1",
        )
        row = (await db.execute(select(GenerationJob))).scalars().one()
        assert row.id == job.id
        assert row.draft_ids == []
        assert row.error is None
        assert row.created_at is not None

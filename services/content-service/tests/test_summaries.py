"""Tests for the session_summaries service layer (save/latest)."""

from uuid import UUID

from sqlalchemy import select

from app import services as summary_services
from app.models import SessionSummary

SESSION_ID = UUID("11111111-1111-1111-1111-111111111111")
JOB_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _merged(**overrides):
    payload = {
        "session_summary": "The party reaches the gates of Moria.",
        "characters": [{"name": "Aragorn", "mentions": 3}],
        "locations": [{"name": "Moria", "mentions": 2}],
        "events": [{"title": "Entering Moria", "participants": ["Aragorn"]}],
        "timeline_entries": [{"time": "00:00:12", "summary": "The gate opens."}],
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


async def test_save_then_latest(session_factory):
    async with session_factory() as db:
        row = await summary_services.save_summary(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged=_merged(),
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v1",
        )
        assert row.id is not None
        assert row.summary == "The party reaches the gates of Moria."
        assert row.events[0]["title"] == "Entering Moria"
        assert row.timeline_entries[0]["time"] == "00:00:12"
        assert float(row.confidence) == 0.8

        latest = await summary_services.latest_summary_for_session(db, SESSION_ID)
        assert latest is not None
        assert latest.id == row.id


async def test_save_overwrites_previous_run(session_factory):
    async with session_factory() as db:
        await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID,
            merged=_merged(session_summary="First run."),
            llm_provider="deepseek", llm_model="m1", prompt_version="v1",
        )
        await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID,
            merged=_merged(session_summary="Second run."),
            llm_provider="deepseek", llm_model="m2", prompt_version="v2",
        )
        rows = (await db.execute(select(SessionSummary))).scalars().all()
        assert len(rows) == 1  # unique per session: one row, overwritten
        assert rows[0].summary == "Second run."
        assert rows[0].llm_model == "m2"
        assert rows[0].prompt_version == "v2"


async def test_latest_unknown_session(session_factory):
    async with session_factory() as db:
        assert await summary_services.latest_summary_for_session(
            db, UUID("99999999-9999-9999-9999-999999999999")
        ) is None

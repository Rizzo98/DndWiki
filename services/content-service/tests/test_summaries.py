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

# ------------------------------------------------- the review layer contract


async def test_save_writes_a_draft_and_resets_a_previous_confirmation(session_factory):
    """Regenerating always sends the session back to the DM for review."""
    async with session_factory() as db:
        await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID, merged=_merged(),
            llm_provider="deepseek", llm_model="m1", prompt_version="v1",
            party_characters=["Aragorn"],
        )
        confirmed = await summary_services.confirm_summary(db, SESSION_ID, confirmed_by=JOB_ID)
        assert confirmed is not None
        assert confirmed.review_status == "confirmed"
        assert confirmed.confirmed_at is not None

        fresh = await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID, merged=_merged(session_summary="New draft."),
            llm_provider="deepseek", llm_model="m1", prompt_version="v1",
        )
    assert fresh.review_status == "draft"
    assert fresh.revision == 1
    assert fresh.confirmed_at is None
    assert fresh.confirmed_by is None
    assert fresh.edit_history == []


async def test_apply_revision_bumps_the_revision_and_keeps_the_draft(session_factory):
    async with session_factory() as db:
        await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID,
            merged=_merged(language="it", confidence=0.8),
            llm_provider="deepseek", llm_model="m1", prompt_version="v1",
            party_characters=["Aragorn"],
        )
        row = await summary_services.apply_revision(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged={"session_summary": "Rewritten line.", "confidence": 0.8},
            llm_provider="deepseek",
            llm_model="m1",
            prompt_version="v1",
            edits=[{"instruction": "fix it", "targets": ["old line"]}],
        )
    assert row.revision == 2
    assert row.review_status == "draft"
    assert row.summary == "Rewritten line."
    assert row.language == "it"  # untouched by a text-only rewrite
    assert row.party_characters == ["Aragorn"]
    assert row.edit_history[0]["instruction"] == "fix it"


async def test_apply_revision_without_a_summary_raises(session_factory):
    import pytest

    async with session_factory() as db:
        with pytest.raises(ValueError, match="no summary to revise"):
            await summary_services.apply_revision(
                db, SESSION_ID, generation_job_id=JOB_ID,
                merged={"session_summary": "x"},
                llm_provider="deepseek", llm_model="m1", prompt_version="v1",
            )


async def test_summary_to_merged_rebuilds_the_extraction(session_factory):
    """Phase 2 consumes the confirmed summary, not the transcript."""
    async with session_factory() as db:
        await summary_services.save_summary(
            db, SESSION_ID, generation_job_id=JOB_ID,
            merged=_merged(language="it", confidence=0.8),
            llm_provider="deepseek", llm_model="m1", prompt_version="v1",
            party_characters=["Aragorn"],
        )
        row = await summary_services.latest_summary_for_session(db, SESSION_ID)
    merged = summary_services.summary_to_merged(row)
    assert merged["language"] == "it"
    assert merged["session_summary"] == "The party reaches the gates of Moria."
    assert merged["characters"][0]["name"] == "Aragorn"
    assert merged["events"][0]["title"] == "Entering Moria"
    assert merged["confidence"] == 0.8
    assert merged["party_characters"] == ["Aragorn"]


def test_summary_lines_splits_and_trims():
    assert summary_services.summary_lines("a\n  b  \n\nc") == ["a", "b", "c"]
    assert summary_services.summary_lines("") == []

# --------------------------------------------------------------------------
# the conflict flag on the row (v18)
# --------------------------------------------------------------------------


def _a_conflict() -> dict:
    return {
        "score": 0.45,
        "first": {"text": "Il gruppo entra nella stanza", "from": "u_00436", "to": "u_00595"},
        "second": {"text": "Entrati nella stanza, il gruppo", "from": "u_00596", "to": "u_00649"},
    }


async def test_save_persists_the_conflict_flag(session_factory):
    """The beats are not persisted - they are scaffolding for the compose call -
    so this flag is all that survives them to reach the DM."""
    async with session_factory() as db:
        row = await summary_services.save_summary(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged=_merged(conflicts=[_a_conflict()]),
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v18",
        )
        assert row.conflicts == [_a_conflict()]


async def test_a_summary_with_no_conflicts_stores_an_empty_list(session_factory):
    async with session_factory() as db:
        row = await summary_services.save_summary(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged=_merged(),
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v18",
        )
        assert row.conflicts == []


async def test_a_revision_clears_the_flag(session_factory):
    """The flag describes the BEATS, and a revision does not recompute them: the
    DM has read the passage and said what to write instead. Keeping it would
    leave the page contradicting the text it sits next to."""
    async with session_factory() as db:
        await summary_services.save_summary(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged=_merged(conflicts=[_a_conflict()]),
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v18",
        )
        row = await summary_services.apply_revision(
            db,
            SESSION_ID,
            generation_job_id=JOB_ID,
            merged=_merged(session_summary="The party stands before the gate."),
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v18",
        )
        assert row.conflicts == []

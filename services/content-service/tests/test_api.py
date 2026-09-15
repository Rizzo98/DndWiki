"""Tests for the FastAPI endpoints (health, llm config, job + summary status)."""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import db as dnd_db
from sqlalchemy.ext.asyncio import AsyncSession

from app import services as job_services
from app.main import app
from app.services import summaries


@pytest.fixture
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_llm_config(client):
    resp = await client.get("/api/content/llm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "deepseek"
    assert body["model"] == "deepseek/deepseek-chat"
    assert body["prompt_version"] == "v12"


async def test_job_status_unknown_session(client):
    resp = await client.get("/api/content/jobs/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 200
    assert resp.json()["job"] is None


async def test_job_status_returns_latest(client, session_factory):
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    async with session_factory() as db:
        await job_services.create_job(
            db, session_id, provider="litellm", model="openai/gpt-4o-mini", prompt_version="v1"
        )

    resp = await client.get(f"/api/content/jobs/{session_id}")
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["status"] == "running"
    assert job["prompt_version"] == "v1"
    assert job["draft_ids"] == []


async def test_job_status_invalid_uuid(client):
    resp = await client.get("/api/content/jobs/not-a-uuid")
    assert resp.status_code == 200
    assert resp.json()["job"] is None


async def test_summary_unknown_session(client):
    resp = await client.get("/api/content/summaries/11111111-1111-1111-1111-111111111111")
    assert resp.status_code == 200
    assert resp.json()["summary"] is None


async def test_summary_invalid_uuid(client):
    resp = await client.get("/api/content/summaries/not-a-uuid")
    assert resp.status_code == 200
    assert resp.json()["summary"] is None


async def test_summary_returns_persisted_row(client, session_factory):
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    job_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    async with session_factory() as db:
        await summaries.save_summary(
            db,
            session_id,
            generation_job_id=job_id,
            merged={
                "session_summary": "The party reaches the gates of Moria.",
                "characters": [{"name": "Aragorn", "mentions": 3}],
                "locations": [],
                "events": [{"title": "Entering Moria", "participants": ["Aragorn"]}],
                "timeline_entries": [{"time": "00:00:12", "summary": "The gate opens."}],
                "confidence": 0.8,
            },
            llm_provider="deepseek",
            llm_model="deepseek/deepseek-chat",
            prompt_version="v1",
        )

    resp = await client.get(f"/api/content/summaries/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == str(session_id)
    summary = body["summary"]
    assert summary is not None
    assert summary["summary"] == "The party reaches the gates of Moria."
    assert summary["generation_job_id"] == str(job_id)
    assert summary["events"][0]["title"] == "Entering Moria"
    assert summary["timeline_entries"][0]["time"] == "00:00:12"
    assert summary["confidence"] == 0.8
    assert summary["llm_provider"] == "deepseek"
    assert summary["llm_model"] == "deepseek/deepseek-chat"
    # a freshly saved summary is the DM's draft, revision 1
    assert summary["review_status"] == "draft"
    assert summary["revision"] == 1
    assert summary["confirmed_at"] is None
    assert summary["confirmed_by"] is None

"""DELETE /internal/content/sessions/{id}: purge a deleted session's rows.

session-service calls this when the DM deletes a session. It drops the session's
summary, generation jobs and proposed change set, and REFUSES (409) while the
session's content is already in the wiki — a deleted session must not leave
pages behind that point at it.
"""

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dnd_common import db as dnd_db
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import internal as internal_api
from app.clients.wiki_service import WikiServiceError
from app.deps import require_service
from app.main import app
from app.models import GenerationJob, SessionSummary, WikiChangeSet
from app.services import jobs, plans, summaries
from tests.conftest import SESSION_ID, make_extraction


class FakeWikiClient:
    """Reports what a session wrote into the wiki (or fails to answer)."""

    def __init__(self, content: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.content = content or {"pages": [], "timeline": [], "published": 0}
        self.error = error

    async def session_content(self, session_id: str) -> dict:
        self.calls.append(session_id)
        if self.error is not None:
            raise self.error
        return self.content


@pytest.fixture
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    app.dependency_overrides[require_service] = lambda: None
    internal_api._wiki_client = FakeWikiClient()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._wiki = internal_api._wiki_client  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


async def _seed_session_rows(session_factory, *, applied: bool = False):
    """A generation job, a draft summary and a proposed change set."""
    async with session_factory() as db:
        job = await jobs.create_job(
            db, UUID(SESSION_ID), provider="deepseek", model="deepseek-chat", prompt_version="v11"
        )
        await summaries.save_summary(
            db,
            UUID(SESSION_ID),
            generation_job_id=job.id,
            merged=make_extraction(),
            llm_provider="deepseek",
            llm_model="deepseek-chat",
            prompt_version="v11",
        )
        plan = await plans.save_plan(
            db,
            UUID(SESSION_ID),
            summary_id=None,
            generation_job_id=job.id,
            change_set={"changes": [{"id": "c1"}], "relations": [], "skipped": []},
            language="en",
        )
        if applied:
            await plans.mark_applying(db, UUID(SESSION_ID))
            await plans.mark_applied(db, UUID(SESSION_ID))
        return job.id, plan.id


async def _counts(session_factory) -> dict[str, int]:
    from sqlalchemy import func, select

    async with session_factory() as db:
        return {
            table.__tablename__: await db.scalar(
                select(func.count()).select_from(table).where(table.session_id == UUID(SESSION_ID))
            )
            for table in (GenerationJob, SessionSummary, WikiChangeSet)
        }


async def test_purge_drops_every_row_of_the_session(client, session_factory):
    await _seed_session_rows(session_factory)

    resp = await client.delete(f"/internal/content/sessions/{SESSION_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["jobs"] == 1
    assert body["summaries"] == 1
    assert body["change_sets"] == 1
    assert await _counts(session_factory) == {
        "generation_jobs": 0,
        "session_summaries": 0,
        "wiki_change_sets": 0,
    }
    # the wiki was asked what the session wrote
    assert client._wiki.calls == [SESSION_ID]  # type: ignore[attr-defined]


async def test_purge_is_idempotent(client, session_factory):
    resp = await client.delete(f"/internal/content/sessions/{SESSION_ID}")
    assert resp.status_code == 200
    assert resp.json()["jobs"] == 0


async def test_purge_refused_when_the_change_set_was_applied(client, session_factory):
    await _seed_session_rows(session_factory, applied=True)

    resp = await client.delete(f"/internal/content/sessions/{SESSION_ID}")
    assert resp.status_code == 409
    assert "already generated" in resp.json()["detail"]
    # nothing was discarded
    assert await _counts(session_factory) == {
        "generation_jobs": 1,
        "session_summaries": 1,
        "wiki_change_sets": 1,
    }


async def test_purge_refused_when_the_wiki_holds_the_session_content(
    client, session_factory
):
    await _seed_session_rows(session_factory)
    client._wiki.content = {  # type: ignore[attr-defined]
        "pages": [
            {"id": str(UUID(int=1)), "title": "Aragorn", "kind": "character",
             "status": "published"},
        ],
        "timeline": [{"id": str(UUID(int=2)), "summary": "The gate opens", "approved": True}],
        "published": 1,
    }

    resp = await client.delete(f"/internal/content/sessions/{SESSION_ID}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Aragorn" in detail and "timeline entr" in detail
    assert await _counts(session_factory) == {
        "generation_jobs": 1,
        "session_summaries": 1,
        "wiki_change_sets": 1,
    }


async def test_purge_503_when_wiki_service_is_unreachable(client, session_factory):
    client._wiki.error = WikiServiceError("wiki-service unreachable")  # type: ignore[attr-defined]
    await _seed_session_rows(session_factory)

    resp = await client.delete(f"/internal/content/sessions/{SESSION_ID}")
    assert resp.status_code == 503
    assert await _counts(session_factory) == {
        "generation_jobs": 1,
        "session_summaries": 1,
        "wiki_change_sets": 1,
    }


async def test_purge_requires_a_service_token(session_factory):
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[dnd_db.get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.delete(f"/internal/content/sessions/{SESSION_ID}")
    app.dependency_overrides.clear()
    assert resp.status_code == 401

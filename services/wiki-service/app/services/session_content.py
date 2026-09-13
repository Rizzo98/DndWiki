"""Session-scoped wiki content: what a session wrote, and whether it blocks deletion.

Session-service never touches wiki tables; before it deletes a session it asks
content-service, which asks here: a session whose pages are already in the wiki
must NOT be deletable, otherwise the pages would keep pointing at a session
that no longer exists. Nothing here writes: this listing is the guard, the DM
decides what to do with the content itself (archive the page, edit it, ...).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TimelineEvent, WikiPage
from app.services.pages import PUBLISHED


async def session_content(db: AsyncSession, session_id: UUID) -> dict:
    """Every page and timeline entry attributed to a session.

    'published' counts the pages that are live in the wiki: those are the ones
    that make a session undeletable (a draft can be thrown away with it).
    """
    pages = list(
        (
            await db.execute(
                select(WikiPage)
                .where(WikiPage.source_session_id == session_id)
                .order_by(WikiPage.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = list(
        (
            await db.execute(
                select(TimelineEvent)
                .where(TimelineEvent.source_session_id == session_id)
                .order_by(TimelineEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "session_id": str(session_id),
        "pages": [
            {
                "id": str(page.id),
                "title": page.title,
                "kind": page.kind,
                "status": page.status,
            }
            for page in pages
        ],
        "timeline": [
            {
                "id": str(event.id),
                "summary": event.summary,
                "approved": bool(event.approved),
            }
            for event in events
        ],
        "published": sum(1 for page in pages if page.status == PUBLISHED),
    }

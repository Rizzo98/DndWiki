"""Apply a DM-confirmed change set to the wiki (internal pipeline endpoint).

This is the ONE path that writes pipeline-generated content as PUBLISHED. The
public API still restricts service tokens to draft|pending_review: publishing
there requires the DM to approve each draft. Here the DM already reviewed the
whole change set on the session page (the 'git status' of the session) and
confirmed it, so the pages land in the wiki ready to read — no 'pending
review' state is ever created by the pipeline.

Everything here is idempotent enough to be safe on a retried message:

- a create whose page the campaign already documents (same kind + title, or a
  page this very session already created) is SKIPPED and reported, never
  duplicated;
- an update rewrites the same fields with the same values;
- a timeline entry is refreshed, and its approval state is never flipped: a
  NEW entry is created approved (the DM confirmed it), an existing one keeps
  whatever the DM decided about it.
"""

from __future__ import annotations

import logging
from typing import Any

from dnd_common.events import Event
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker import EventPublisher
from app.models import PageRelation
from app.schemas import ChangeSetApply, PlannedPage
from app.services import pages as page_services
from app.services import timeline as timeline_services

logger = logging.getLogger(__name__)


def _title_key(title: str) -> str:
    return " ".join((title or "").lower().split())


def _campaign_index(pages: list) -> tuple[dict[str, Any], dict[tuple[str, str], Any]]:
    """(title/alias -> page, (kind, title) -> page) over the campaign's pages."""
    by_name: dict[str, Any] = {}
    by_kind_title: dict[tuple[str, str], Any] = {}
    for page in pages:
        by_kind_title.setdefault((page.kind, _title_key(page.title)), page)
        names = [page.title, *((page.content_json or {}).get("aliases") or [])]
        for name in names:
            if isinstance(name, str) and name.strip():
                by_name.setdefault(_title_key(name), page)
    return by_name, by_kind_title


async def apply_change_set(
    db: AsyncSession, body: ChangeSetApply, *, publisher: EventPublisher
) -> dict[str, Any]:
    """Write a confirmed change set; returns what was created/updated/skipped."""
    existing = [
        page
        for page in await page_services.list_pages(db, body.campaign_id, "dm", limit=500)
        if page.status != page_services.ARCHIVED
    ]
    by_name, by_kind_title = _campaign_index(existing)
    # Pages created by THIS call, so relations can resolve their titles.
    created_by_title: dict[str, Any] = {}

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for change in body.changes:
        if change.action == "update":
            await _apply_update(db, body, change, publisher, updated, skipped)
            continue

        key = (change.kind, _title_key(change.title))
        clash = by_kind_title.get(key) or created_by_title.get(_title_key(change.title))
        if clash is not None:
            skipped.append(
                {
                    "change_id": change.change_id,
                    "page_id": str(clash.id),
                    "title": getattr(clash, "title", change.title),
                    "kind": change.kind,
                    "action": "create",
                    "reason": "the campaign already documents this page",
                }
            )
            continue

        page = await page_services.create_page(
            db,
            campaign_id=body.campaign_id,
            kind=change.kind,
            title=change.title,
            content_json=change.content_json,
            status=page_services.PUBLISHED,
            visibility=change.visibility,
            confidence=change.confidence,
            source_session_id=body.session_id,
            created_by=body.confirmed_by,
            change_note=change.change_note
            or f"Confirmed change set of session {body.session_id}",
        )
        created_by_title[_title_key(page.title)] = page
        by_kind_title[key] = page
        for name in [page.title, *((page.content_json or {}).get("aliases") or [])]:
            if isinstance(name, str) and name.strip():
                by_name.setdefault(_title_key(name), page)
        created.append(
            {
                "change_id": change.change_id,
                "page_id": str(page.id),
                "title": page.title,
                "kind": page.kind,
                "action": "create",
                "reason": None,
            }
        )
        await publisher.publish(
            Event(type="wiki.published", payload=page_services.page_event_payload(page))
        )

    # Timeline entries: every event page (new or updated) backs one. New
    # entries are created APPROVED (the DM confirmed the change), existing
    # entries keep their approval state.
    timeline_written = 0
    resolved: dict[str, Any] = {**by_name, **created_by_title}
    resolved_by_kind_title = {**by_kind_title}
    for change in body.changes:
        if change.timeline is None:
            continue
        page = await _resolve_page(db, change, resolved, resolved_by_kind_title)
        if page is None:
            skipped.append(
                {
                    "change_id": change.change_id,
                    "page_id": None,
                    "title": change.title,
                    "kind": change.kind,
                    "action": change.action,
                    "reason": "no page to attach the timeline entry to",
                }
            )
            continue
        await timeline_services.upsert_timeline_event(
            db,
            campaign_id=body.campaign_id,
            page_id=page.id,
            in_world_date=change.timeline.in_world_date,
            summary=change.timeline.summary,
            source_session_id=body.session_id,
            approved_on_create=True,
        )
        timeline_written += 1

    relations_created = await _apply_relations(
        db, body, resolved=resolved, resolved_by_kind_title=resolved_by_kind_title
    )
    logger.info(
        "change set of session %s applied: %d created, %d updated, %d skipped, "
        "%d timeline entries, %d relations",
        body.session_id, len(created), len(updated), len(skipped), timeline_written,
        relations_created,
    )
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "timeline_entries": timeline_written,
        "relations_created": relations_created,
    }


async def _resolve_page(
    db: AsyncSession, change: PlannedPage, by_name: dict, by_kind_title: dict
):
    """The page a change refers to: its id when it has one, else its title."""
    if change.page_id is not None:
        page = await page_services.get_page(db, change.page_id)
        if page is not None:
            return page
    return by_kind_title.get((change.kind, _title_key(change.title))) or by_name.get(
        _title_key(change.title)
    )


async def _apply_update(
    db: AsyncSession,
    body: ChangeSetApply,
    change: PlannedPage,
    publisher: EventPublisher,
    updated: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> None:
    """Rewrite an existing page with the confirmed content."""
    page = await page_services.get_page(db, change.page_id) if change.page_id else None
    if page is None:
        skipped.append(
            {
                "change_id": change.change_id,
                "page_id": str(change.page_id) if change.page_id else None,
                "title": change.title,
                "kind": change.kind,
                "action": "update",
                "reason": "the page no longer exists",
            }
        )
        return
    previous_title = page.title
    # what the DM reviewed is what gets written: the change already carries the
    # merged content, an empty payload never blanks a page
    await page_services.update_page(
        db,
        page.id,
        title=change.title or page.title,
        content_json=change.content_json or (page.content_json or {}),
        change_note=change.change_note
        or f"Updated from the confirmed change set of session {body.session_id}",
        updated_by=body.confirmed_by,
    )
    updated.append(
        {
            "change_id": change.change_id,
            "page_id": str(page.id),
            "title": page.title,
            "kind": page.kind,
            "action": "update",
            "reason": None if previous_title == page.title else f"was '{previous_title}'",
        }
    )
    if page.status == page_services.PUBLISHED:
        await publisher.publish(
            Event(type="wiki.updated", payload=page_services.page_event_payload(page))
        )


async def _apply_relations(
    db: AsyncSession,
    body: ChangeSetApply,
    *,
    resolved: dict[str, Any],
    resolved_by_kind_title: dict,
) -> int:
    """Create the proposed cross-references, resolving titles to pages."""
    created = 0
    for relation in body.relations:
        from_page = resolved.get(_title_key(relation.from_title))
        if from_page is None:
            logger.warning(
                "change set of session %s: relation source '%s' has no page",
                body.session_id, relation.from_title,
            )
            continue
        to_page = None
        if relation.to_page_id is not None:
            to_page = await page_services.get_page(db, relation.to_page_id)
        elif relation.to_title:
            to_page = resolved.get(_title_key(relation.to_title))
        if to_page is None or to_page.id == from_page.id:
            continue
        if to_page.campaign_id != from_page.campaign_id:
            continue
        exists = await db.scalar(
            select(PageRelation.id).where(
                PageRelation.page_id == from_page.id,
                PageRelation.related_page_id == to_page.id,
                PageRelation.relation_type == relation.relation_type,
            )
        )
        if exists is not None:
            continue
        try:
            await page_services.create_relation(
                db, from_page.id, to_page.id, relation.relation_type
            )
            created += 1
        except HTTPException as exc:  # duplicate/deleted target: keep going
            logger.warning(
                "change set of session %s: relation %s -> %s failed: %s",
                body.session_id, relation.from_title, relation.to_title or relation.to_page_id,
                exc.detail,
            )
    return created

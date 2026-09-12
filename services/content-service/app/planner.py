"""Build the PROPOSED wiki change set of a session (the 'git status').

The confirmed session summary is not written to the wiki directly: it is
mapped into a reviewable list of changes — one entry per page to create, page
to update and timeline entry to write, plus the cross-references proposed
between them. The DM inspects/edits/drops single entries on the session page
and confirms the whole set; only then does the worker apply it through
wiki-service (see app/workers/generate.py, phase 3).

Each change carries:

- 'action': 'create' (a page the campaign does not document yet) or 'update'
  (an existing page — typically an event page — gaining new information);
- 'after': the payload that would be written (title, content_json,
  visibility, confidence);
- 'before': for updates, the page's CURRENT title/content_json, so the UI can
  render a per-field diff instead of a blind preview;
- 'timeline': for event pages, the timeline entry to create/refresh
  (summary + in-world date);
- 'dropped': the DM unchecked it — kept in the set so the review is
  reversible.

Entities the campaign already documents (exact title/alias match) are NOT
changes: they land in 'skipped' and stay visible on the session summary.
"""

from __future__ import annotations

from typing import Any

from app.merger import build_event_drafts, build_page_drafts


def _lookup_page(existing_pages: list[dict[str, Any]], page_id: str) -> dict[str, Any] | None:
    for page in existing_pages or []:
        if str(page.get("id") or "") == page_id:
            return page
    return None


def build_change_set(
    merged: dict[str, Any],
    campaign_id: str,
    session_id: str,
    *,
    existing_pages: list[dict[str, Any]] | None = None,
    party_characters: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Map a merged extraction to the reviewable change set of a session.

    Returns {'changes', 'relations', 'skipped'}; ids are deterministic within
    a set ('c1', 'r1', ...) so the UI can edit single entries and send them
    back without inventing identifiers.
    """
    existing_pages = existing_pages or []
    drafts, relations, duplicates = build_page_drafts(
        merged,
        campaign_id,
        session_id,
        existing_pages=existing_pages,
        party_characters=party_characters,
    )
    event_drafts, event_updates, timeline_events, event_duplicates = build_event_drafts(
        merged, campaign_id, session_id, existing_pages=existing_pages
    )

    # Every event (new or updated) backs a timeline entry; index them so the
    # event changes can carry theirs.
    timeline_by_title = {entry["title"]: entry for entry in timeline_events}
    timeline_by_page = {
        str(entry["page_id"]): entry for entry in timeline_events if entry.get("page_id")
    }

    changes: list[dict[str, Any]] = []
    counter = 0

    def _add_create(draft: dict[str, Any], timeline: dict[str, Any] | None) -> None:
        nonlocal counter
        counter += 1
        changes.append(
            {
                "id": f"c{counter}",
                "action": "create",
                "kind": draft["kind"],
                "title": draft["title"],
                "page_id": None,
                "before": None,
                "after": {
                    "title": draft["title"],
                    "content_json": draft.get("content_json") or {},
                    "visibility": draft.get("visibility") or "public",
                    "confidence": draft.get("confidence"),
                },
                "timeline": (
                    {
                        "summary": timeline["summary"],
                        "in_world_date": timeline.get("in_world_date"),
                    }
                    if timeline
                    else None
                ),
                "dropped": False,
            }
        )

    for draft in drafts:
        _add_create(draft, None)
    for draft in event_drafts:
        _add_create(draft, timeline_by_title.get(draft["title"]))

    for update in event_updates:
        counter += 1
        page = _lookup_page(existing_pages, str(update["page_id"]))
        timeline = timeline_by_page.get(str(update["page_id"]))
        changes.append(
            {
                "id": f"c{counter}",
                "action": "update",
                "kind": "event",
                "title": update["title"],
                "page_id": str(update["page_id"]),
                "before": {
                    "title": (page or {}).get("title") or update["title"],
                    "content_json": (page or {}).get("content_json") or {},
                },
                "after": {
                    "title": (page or {}).get("title") or update["title"],
                    "content_json": update.get("content_json") or {},
                    "visibility": (page or {}).get("visibility") or "public",
                    "confidence": None,
                },
                "timeline": (
                    {
                        "summary": timeline["summary"],
                        "in_world_date": timeline.get("in_world_date"),
                    }
                    if timeline
                    else None
                ),
                "dropped": False,
            }
        )

    planned_relations: list[dict[str, Any]] = []
    for index, rel in enumerate(relations, start=1):
        planned_relations.append(
            {
                "id": f"r{index}",
                "from_title": rel["from_title"],
                "to_title": rel.get("to_title"),
                "to_page_id": str(rel["to_page_id"]) if rel.get("to_page_id") else None,
                "relation_type": rel["relation_type"],
                "dropped": False,
            }
        )

    skipped = [
        {
            "title": duplicate["title"],
            "kind": duplicate["kind"],
            "matched_title": duplicate.get("matched_title"),
            "reason": "already documented by this campaign",
        }
        for duplicate in [*duplicates, *event_duplicates]
    ]

    return {"changes": changes, "relations": planned_relations, "skipped": skipped}

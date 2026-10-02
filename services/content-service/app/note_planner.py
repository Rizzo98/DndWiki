"""Turn what the LLM read out of a DM's notes into a reviewable change set.

A note is prose the DM wrote for themselves, so nothing in it is structured
yet: the model's job is to say WHICH pages the notes are about and what each
of them should say. Everything after that is decided here, deterministically,
for two reasons the codebase already relies on elsewhere:

- whether a page is CREATED or UPDATED is a question about the campaign, not
  about the notes. The model is shown the campaign's pages for context, but the
  decision is made by matching the proposed title against them
  (merger.classify_existing_page: same name, then near name, both of which mean
  the campaign documents this entity) - the same rule a session's extraction is
  put through, so a note and a session agree about what is new;
- what the model returns is untrusted. Kinds, titles, prose and attributes all
  pass through a sanitizer before they become a change, because the apply is
  all-or-nothing: one malformed attribute would cost the DM the whole proposal.

The change set it produces is the shape the session pipeline already reviews
(app/planner.py): {changes, relations, skipped}, ids 'c1'/'r1'..., 'before' for
updates so the plan page can diff, 'dropped' false. The plan page renders it
with the same components.
"""

from __future__ import annotations

from typing import Any

from app.merger import (
    MATCH_RELATED,
    MATCH_SAME_ENTITY,
    MATCH_SAME_NAME,
    _merged_entity_content,
    classify_existing_page,
    known_names_for_page,
)
from app.note_attributes import sanitize_content_json

#: The wiki's six page kinds. A proposal naming anything else is dropped: the
#: column is free text but the schema is a closed set (wiki-service
#: schemas.PAGE_KINDS).
PAGE_KINDS = ("character", "location", "faction", "item", "quest", "event")

#: Cross-references the notes may propose. The wiki stores the type as free
#: text; this is the vocabulary the session pipeline uses, and 'related_to' is
#: the honest default for a link nobody described.
RELATION_TYPES = (
    "appears_in",
    "member_of",
    "allied_with",
    "led_by",
    "owner",
    "related_to",
)
DEFAULT_RELATION_TYPE = "related_to"

MAX_TITLE = 255
MAX_CHANGES = 200
MAX_RELATIONS = 200


def _title_key(title: str) -> str:
    return " ".join((title or "").lower().split())


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _clean_title(value: Any) -> str:
    return (value.strip() if isinstance(value, str) else "")[:MAX_TITLE]


def _clean_kind(value: Any) -> str | None:
    kind = (value.strip().lower() if isinstance(value, str) else "")
    return kind if kind in PAGE_KINDS else None


def _timeline_for(kind: str, title: str, content: dict[str, Any]) -> dict[str, Any] | None:
    """The timeline entry an event page backs.

    A note that describes something that HAPPENED belongs on the campaign's
    timeline: an event page with no entry behind it is invisible in the one
    view built for chronology. The entry needs a summary and takes the in-world
    date the notes gave the event, which is exactly what the DM would have had
    to type twice otherwise.
    """
    if kind != "event":
        return None
    attributes = content.get("attributes") or {}
    summary = content.get("summary") or title
    return {
        "summary": str(summary)[:2000] or title,
        "in_world_date": attributes.get("in_world_date"),
    }


def build_change_set(
    proposal: Any,
    *,
    existing_pages: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Map the model's reading of the notes to a reviewable change set.

    Returns {'changes', 'relations', 'skipped'}; a proposal that carries nothing
    usable yields an empty change set rather than an error - the DM sees "the
    notes proposed no page" instead of a failed generation.
    """
    existing_pages = existing_pages or []
    body = proposal if isinstance(proposal, dict) else {}
    raw_pages = body.get("pages") if isinstance(body.get("pages"), list) else []

    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [
        {
            "title": _clean_title(entry.get("title")) or "?",
            "reason": (str(entry.get("reason") or "").strip() or "nothing to write")[:500],
        }
        for entry in (body.get("skipped") or [])
        if isinstance(entry, dict)
    ]
    # Proposed pages, keyed so a second proposal for the same page folds into
    # the first instead of becoming a competing change.
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    # title/alias (normalized) -> change, for resolving the relations below.
    by_name: dict[str, dict[str, Any]] = {}
    # (change id, proposed title, page whose name contains it): a page this
    # proposal may duplicate, linked once both end up with a change.
    duplicate_hints: list[tuple[str, str, dict[str, Any]]] = []
    counter = 0

    for raw in raw_pages[:MAX_CHANGES]:
        if not isinstance(raw, dict):
            continue
        kind = _clean_kind(raw.get("kind"))
        title = _clean_title(raw.get("title"))
        if not kind or not title:
            skipped.append(
                {
                    "title": title or "?",
                    "reason": "the proposal did not name a page kind the wiki has",
                }
            )
            continue
        aliases = [
            name
            for name in _strings(raw.get("also_known_as"))
            if _title_key(name) != _title_key(title)
        ][:16]
        content = sanitize_content_json(kind, raw.get("content"))

        key = (kind, _title_key(title))
        if key in by_key:
            # The model split one page across two entries: fold them.
            existing_change = by_key[key]
            existing_change["after"]["content_json"] = _merged_entity_content(
                existing_change["after"]["content_json"],
                content,
                page_title=existing_change["title"],
                fresh_title=title,
            )
            continue

        page, how = classify_existing_page(title, aliases, existing_pages, kind=kind)
        is_update = page is not None and how in (MATCH_SAME_NAME, MATCH_SAME_ENTITY)
        # A page whose name merely CONTAINS this one is not this entity: the note
        # is about something the campaign has not written down yet and the two
        # may want merging later. Recorded as a hint - the same
        # 'possible_duplicate' link the session planner proposes - and turned
        # into a relation once both ends have a change to hang off.
        related_to = page if (page is not None and how == MATCH_RELATED) else None
        # Nothing of THIS kind is close, but the campaign may still document a
        # page of ANOTHER kind under the very same name (the subject was filed
        # differently, or the name is genuinely shared). The session planner
        # SKIPS that case, because a name it extracted under the wrong kind is
        # usually the same entity mis-filed. A note is deliberate, so the page is
        # still created instead - wiki-service gives it its own slug ("bree" then
        # "bree-2") and the two do not collide - and the pair is linked with the
        # same 'possible_duplicate' hint the containment case gets, so the DM can
        # merge them if they turn out to be one thing.
        if page is None:
            other, other_how = classify_existing_page(title, aliases, existing_pages)
            if other is not None and other_how == MATCH_SAME_NAME:
                related_to = other

        # An update's payload is the page's own content with this note folded in,
        # computed BEFORE an id is spent: the fold can turn out to add nothing.
        page_title = title
        before_content: dict[str, Any] = {}
        after_content: dict[str, Any] = content
        if is_update:
            page_title = page.get("title") or title
            before_content = page.get("content_json") or {}
            # A page keeps the name the campaign reads; the name the NOTES used
            # becomes an alias, which is what stops the next generation from
            # proposing a second page for the same thing.
            after_content = _merged_entity_content(
                before_content,
                {**content, "aliases": [*_strings(content.get("aliases")), *aliases, title]},
                page_title=page_title,
                fresh_title=title,
            )
            if after_content == before_content:
                # The page already says everything this note adds. Proposing the
                # rewrite would ask the DM to confirm a page rewritten with its
                # own content, and applying it would write a no-op version and
                # re-index the page. It is context, not a change - the same rule
                # the session planner applies.
                skipped.append(
                    {
                        "title": page_title,
                        "reason": "the page already says this; the note adds nothing new",
                    }
                )
                continue

        counter += 1
        change_id = f"c{counter}"
        if related_to is not None:
            duplicate_hints.append((change_id, title, related_to))

        if is_update:
            change = {
                "id": change_id,
                "action": "update",
                "kind": kind,
                "title": page_title,
                "page_id": str(page.get("id") or "") or None,
                "before": {"title": page_title, "content_json": before_content},
                "after": {
                    "title": page_title,
                    "content_json": after_content,
                    "visibility": page.get("visibility") or "public",
                    "confidence": None,
                },
                "timeline": _timeline_for(kind, page_title, after_content),
                "dropped": False,
            }
        else:
            if aliases:
                content = {**content, "aliases": aliases}
            change = {
                "id": change_id,
                "action": "create",
                "kind": kind,
                "title": title,
                "page_id": None,
                "before": None,
                "after": {
                    "title": title,
                    "content_json": content,
                    "visibility": "public",
                    "confidence": None,
                },
                "timeline": _timeline_for(kind, title, content),
                "dropped": False,
            }
        by_key[key] = change
        changes.append(change)
        for name in [title, *aliases]:
            by_name.setdefault(_title_key(name), change)
        if page is not None:
            for name in known_names_for_page(page):
                by_name.setdefault(_title_key(name), change)

    relations = _resolve_relations(
        body.get("relations"), changes=changes, existing_pages=existing_pages, by_name=by_name
    )
    # A page whose name merely CONTAINS a proposed one is not the same entity, nor
    # is a page of another kind that carries the identical name - but in both
    # cases the DM may well want them merged. The session planner flags the same
    # situations with a 'possible_duplicate' link; this is that flag, proposed
    # only once the pages above exist so both ends resolve.
    #
    # The two titles may be IDENTICAL here, and the link still lands: the apply
    # resolves 'from_title' against the pages this very change set creates and
    # takes 'to_page_id' as given, so the pair resolves to two different pages
    # (verified against the apply endpoint: two pages named "Bree", one slug
    # 'bree' and one 'bree-2', related successfully). What must never happen is a
    # page related to ITSELF, and no hint can produce that - the target is always
    # a page that already exists and the source is always one being created.
    for change_id, title, page in duplicate_hints:
        page_title = page.get("title") or ""
        if not page_title:
            continue
        relations.append(
            {
                "id": f"r{len(relations) + 1}",
                "from_title": title,
                "to_title": page_title,
                "to_page_id": str(page.get("id") or "") or None,
                "relation_type": "possible_duplicate",
                "dropped": False,
                "change_id": change_id,
            }
        )

    return {"changes": changes, "relations": relations, "skipped": skipped}


def _resolve_relations(
    raw_relations: Any,
    *,
    changes: list[dict[str, Any]],
    existing_pages: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-references, with both ends resolved to a page the apply can find.

    wiki-service resolves a relation by title against the pages that exist once
    the change set has been written, so an endpoint pointing at a page that is
    neither created nor already in the wiki is silently dropped there. Dropping
    it here instead is the same outcome without pretending it was proposed.
    """
    if not isinstance(raw_relations, list):
        return []
    existing_by_name: dict[str, dict[str, Any]] = {}
    for page in existing_pages:
        for name in known_names_for_page(page):
            existing_by_name.setdefault(_title_key(name), page)

    resolved: list[dict[str, Any]] = []
    for raw in raw_relations[:MAX_RELATIONS]:
        if not isinstance(raw, dict):
            continue
        from_title = _clean_title(raw.get("from_title"))
        to_title = _clean_title(raw.get("to_title"))
        if not from_title or not to_title or _title_key(from_title) == _title_key(to_title):
            continue
        from_change = by_name.get(_title_key(from_title))
        to_change = by_name.get(_title_key(to_title))
        to_page = existing_by_name.get(_title_key(to_title))
        # The source must be a page this proposal writes; a link between two
        # pages the campaign already has is not something notes can add.
        if from_change is None:
            continue
        if to_change is None and to_page is None:
            continue
        relation_type = str(raw.get("relation_type") or "").strip().lower()
        if relation_type not in RELATION_TYPES:
            relation_type = DEFAULT_RELATION_TYPE
        resolved.append(
            {
                "id": f"r{len(resolved) + 1}",
                "from_title": from_change["title"],
                "to_title": to_page.get("title") if to_page is not None else to_change["title"],
                "to_page_id": (
                    str(to_page.get("id")) if to_page is not None else to_change.get("page_id")
                ),
                "relation_type": relation_type,
                "dropped": False,
            }
        )
    return resolved

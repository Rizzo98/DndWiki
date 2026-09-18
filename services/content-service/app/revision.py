"""The DM's summary review as a PATCH, not a whole-extraction echo.

Since prompt v15 the summary itself is a NARRATIVE in scene blocks
(app/summary.py): the patch carries the whole story back (blocks with their
place labels) and only the extraction items the correction touches, so the DM
can highlight any portion of the prose and have it rewritten without the rest
of the record moving.""

Phase 1b used to ask the model for the COMPLETE corrected extraction: the
revision had to echo every character (41 fields each on a real session), every
location, every event and every timeline entry back. On the reported session
that response is ~23 kB (~8k tokens) while the completion cap is 4096, so the
provider cut it off mid-JSON. Three layers of leniency then hid the truncation:
json_repair closed the partial object into syntactically valid JSON, the client
filled the categories the cut had removed with [], and the worker's guard
restored those empty categories from the PREVIOUS revision. The result was a
corrected summary next to stale events and timeline entries - silently, every
single time (measured: tests/test_extraction.py::test_a_truncated_revision_is_never_silently_accepted).

So the revision is a patch now:

    {"session_summary": "... every line ...",
     "updates":   {"events": {"e1": {"description": "...", "participants": [...]}}},
     "additions": {"characters": [{"name": "...", ...}]},
     "removals":  {"timeline_entries": ["t7"]}}

Every item the model is shown carries an "id" ('c3' is the fourth character,
'e1' the second event, 't7' the eighth timeline entry) and the patch names the
item plus the fields to overwrite. Everything the patch does not mention keeps
the value it already had, so the response is a few hundred tokens instead of
thousands: it fits any session length, and a correction cannot disappear into a
truncated echo any more. What the patch does NOT fit is now a loud error -
unknown item id, a field block that is not an object, a missing summary all
raise SummaryRevisionError, which the worker turns into a failed job the DM can
retry, never into a half-applied revision.
"""

from __future__ import annotations

import logging
from typing import Any

from app.summary import normalize_blocks, summary_from

logger = logging.getLogger(__name__)

#: The extraction categories a revision may touch, in the order they are shown
#: to the model and merged back.
KINDS: tuple[str, ...] = ("characters", "locations", "events", "timeline_entries")

#: Item id prefix per category - how the patch addresses an item.
ITEM_PREFIX: dict[str, str] = {
    "characters": "c",
    "locations": "l",
    "events": "e",
    "timeline_entries": "t",
}

#: The field that names an item. Also the fallback lookup when the model
#: addresses an item by its name/title/time instead of by its id.
IDENTITY_FIELD: dict[str, str] = {
    "characters": "name",
    "locations": "name",
    "events": "title",
    "timeline_entries": "time",
}

#: Identity an ADDED item must carry: an addition without one could never
#: become a page and would be dropped later, silently.
IDENTITY_REQUIRED: dict[str, tuple[str, ...]] = {
    "characters": ("name",),
    "locations": ("name",),
    "events": ("title",),
    "timeline_entries": ("time", "summary"),
}

#: The three sections a patch may carry.
PATCH_SECTIONS: tuple[str, ...] = ("updates", "additions", "removals")

#: Top-level keys the model may echo back from the extraction it was shown (or
#: add as commentary) without them meaning anything: a revision never changes
#: the language, the confidences or the party roster.
ECHOED_KEYS = frozenset({"language", "confidence", "party_characters"})

#: Keys that are plumbing, not data: never merged into an item.
NON_DATA_KEYS = frozenset({"id"})


class SummaryRevisionError(Exception):
    """The model's revision patch does not fit the extraction it came from."""


def item_id(kind: str, index: int) -> str:
    """The id the model uses to address the index-th item of a category."""
    return f"{ITEM_PREFIX[kind]}{index}"


def indexed_for_prompt(extraction: dict[str, Any]) -> dict[str, Any]:
    """The extraction as the model sees it: every item carries its 'id'.

    The ids are assigned here and re-derived by apply_summary_revision from the
    item's POSITION, so they are never persisted: they are an addressing scheme
    for one call, not a new field of the extraction. The narrative is shown as
    its BLOCKS (the shape the answer has to use) instead of the joined text,
    so there is exactly one version of the story in front of the model.
    """
    payload = {key: value for key, value in extraction.items() if key not in KINDS}
    payload["session_summary"] = normalize_blocks(
        payload.get("summary_blocks") or payload.get("session_summary")
    )
    payload.pop("summary_blocks", None)
    for kind in KINDS:
        payload[kind] = [
            {"id": item_id(kind, index), **item}
            for index, item in enumerate(extraction.get(kind) or [])
            if isinstance(item, dict)
        ]
    return payload


def _normalized(value: Any) -> str:
    """Case/whitespace-insensitive form of an identity (lookup only)."""
    return " ".join(str(value or "").split()).casefold()


def _items_of(extraction: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Copies of a category's items: a patch never mutates the extraction it
    was computed from (the worker still holds it for the log/diff)."""
    return [
        dict(item) for item in (extraction.get(kind) or []) if isinstance(item, dict)
    ]


def _resolve(kind: str, items: list[dict[str, Any]], ref: Any) -> int:
    """Index of the item a patch entry addresses.

    By id first (what the prompt asks for), then by the item's own name/title/
    time, because a model that writes the title instead of 'e1' still means the
    same item - and a reference that resolves to NEITHER is an error, never a
    silently dropped correction.
    """
    wanted = str(ref).strip()
    if not wanted:
        raise SummaryRevisionError(f"the patch carries an empty {kind} reference")
    if wanted.casefold() in {item_id(kind, i) for i in range(len(items))}:
        return [item_id(kind, i) for i in range(len(items))].index(wanted.casefold())
    field = IDENTITY_FIELD[kind]
    target = _normalized(wanted)
    for index, item in enumerate(items):
        if target and _normalized(item.get(field)) == target:
            return index
    raise SummaryRevisionError(
        f"the patch addresses {kind} {wanted!r}, which this session does not have "
        f"(ids run from {item_id(kind, 0)} to {item_id(kind, max(len(items) - 1, 0))})"
    )


def _section(patch: dict[str, Any], name: str) -> dict[str, Any]:
    section = patch.get(name) or {}
    if not isinstance(section, dict):
        raise SummaryRevisionError(
            f"'{name}' must be an object, got {type(section).__name__}"
        )
    return section


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """The data fields of a patch object (the addressing keys dropped)."""
    return {
        str(key): value
        for key, value in fields.items()
        if str(key) not in NON_DATA_KEYS and not str(key).startswith("_")
    }


def _assert_patch_shape(patch: Any) -> None:
    """Refuse a patch that is not the one the prompt asked for.

    The strictness is deliberate: a model that answers with the OLD full
    extraction (every category at the top level) must be told so - its answer
    would otherwise be ignored field by field, which is exactly the silent loss
    this protocol exists to remove. LLMClient catches the error and re-asks.
    """
    if not isinstance(patch, dict):
        raise SummaryRevisionError(
            f"the revision must be a JSON object, got {type(patch).__name__}"
        )
    unexpected = sorted(
        str(key)
        for key in patch
        if key not in ("session_summary", *PATCH_SECTIONS)
        and key not in ECHOED_KEYS
        and not str(key).startswith("_")
    )
    if unexpected:
        raise SummaryRevisionError(
            "the patch has unexpected keys ("
            + ", ".join(unexpected)
            + "); it must be {'session_summary': ..., 'updates': ..., "
            "'additions': ..., 'removals': ...} and must NOT repeat the "
            "extraction"
        )
    if not normalize_blocks(patch.get("session_summary")):
        raise SummaryRevisionError(
            "the patch carries no 'session_summary': every revision returns the "
            "COMPLETE narrative, as the list of blocks (or as plain text)"
        )
    for name in PATCH_SECTIONS:
        unknown = sorted(str(k) for k in _section(patch, name) if k not in KINDS)
        if unknown:
            raise SummaryRevisionError(
                f"'{name}' mentions categories this session does not have: "
                + ", ".join(unknown)
            )


def apply_summary_revision(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    """'current' + the model's patch -> the extraction of the next revision.

    Pure: 'current' and its items are left untouched. Raises
    SummaryRevisionError (never returns a half-applied revision) when the patch
    does not fit the extraction.
    """
    _assert_patch_shape(patch)
    updates = _section(patch, "updates")
    additions = _section(patch, "additions")
    removals = _section(patch, "removals")

    merged = dict(current)
    # The narrative is the one thing the model always returns in full: the DM
    # highlighted a passage of it, and the correction has to land in the story
    # itself. Text and blocks come out of the same answer, so they can never
    # disagree (app/summary.py).
    blocks, text = summary_from(patch["session_summary"])
    merged["session_summary"] = text
    merged["summary_blocks"] = blocks
    for kind in KINDS:
        items = _items_of(current, kind)
        for ref, fields in (updates.get(kind) or {}).items():
            if not isinstance(fields, dict):
                raise SummaryRevisionError(
                    f"the update of {kind} {ref!r} is not an object of fields"
                )
            changed = _clean_fields(fields)
            if not changed:
                raise SummaryRevisionError(
                    f"the update of {kind} {ref!r} changes no field"
                )
            items[_resolve(kind, items, ref)].update(changed)
        dropped = {_resolve(kind, items, ref) for ref in (removals.get(kind) or [])}
        if dropped:
            items = [item for index, item in enumerate(items) if index not in dropped]
        for item in additions.get(kind) or []:
            if not isinstance(item, dict):
                raise SummaryRevisionError(f"an added {kind} item is not an object")
            added = _clean_fields(item)
            missing = [
                field
                for field in IDENTITY_REQUIRED[kind]
                if not str(added.get(field) or "").strip()
            ]
            if missing:
                raise SummaryRevisionError(
                    f"an added {kind} item carries no " + ", ".join(missing)
                )
            items.append(added)
        merged[kind] = items
    return merged


def _narrative_blocks(extraction: dict[str, Any]) -> list[dict[str, str]]:
    """The narrative of an extraction, whichever of the two forms it carries."""
    return normalize_blocks(
        extraction.get("summary_blocks") or extraction.get("session_summary")
    )


def describe_revision(before: dict[str, Any], after: dict[str, Any]) -> str:
    """What the patch actually changed, for the worker's log line.

    A DM correction that silently changes nothing is the failure mode this
    module was written for, so every revision says out loud which summary and
    which items moved.
    """
    parts: list[str] = []
    if (before.get("session_summary") or "") != (after.get("session_summary") or ""):
        parts.append("summary rewritten")
    elif _narrative_blocks(before) != _narrative_blocks(after):
        # The prose is identical: what moved is where a part of it happens.
        parts.append("summary labels changed")
    for kind in KINDS:
        field = IDENTITY_FIELD[kind]
        old = {
            _normalized(item.get(field)): item
            for item in (before.get(kind) or [])
            if isinstance(item, dict)
        }
        new = [item for item in (after.get(kind) or []) if isinstance(item, dict)]
        touched = [
            str(item.get(field) or "?")
            for item in new
            if old.get(_normalized(item.get(field))) != item
        ]
        names = {_normalized(item.get(field)) for item in new}
        removed = [
            str(item.get(field) or "?")
            for item in (before.get(kind) or [])
            if isinstance(item, dict) and _normalized(item.get(field)) not in names
        ]
        if touched:
            parts.append(f"{kind} changed: " + ", ".join(touched))
        if removed:
            parts.append(f"{kind} removed: " + ", ".join(removed))
    return "; ".join(parts) if parts else "nothing changed"

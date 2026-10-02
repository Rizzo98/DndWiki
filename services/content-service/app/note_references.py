"""The wiki pages a DM's planning notes REFERENCE with a tag.

A note points at a wiki page by writing "@slug": the toolkit's editor inserts it
when the DM picks a page from the autocomplete
(apps/web/components/page-link-editor.tsx), and the wiki's own fields use "#"
for the same token - the renderer reads the two identically
(apps/web/components/linked-text.tsx), so both are read here.

The grammar is the one the frontend WRITES, not a looser one: a marker followed
by a slug - letters, digits, single hyphens - that is not glued to a letter or a
digit. That last rule is what keeps an e-mail address out of the match, and it
is the rule the renderer applies, so every token resolved here is one the DM
sees as a link.

A tag is a deliberate act: it is the one place in a note where the DM said "this
passage is about THAT page", instead of leaving the pipeline to guess it from a
name that happens to look alike. That is why the tagged page's own content is
read out to the model (render_reference_context): a note about a place the table
has already visited can then say what it ADDS to the page instead of restating
it - the fold in app/note_planner.py never overwrites, so a restatement is a
no-op and would be dropped before the DM ever saw it.

Nothing is written here: this module only answers "which pages does this text
point at, and what do they currently say".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.note_attributes import PROSE_FIELDS

#: A letter or a digit (unicode-aware, but not an underscore): the "not part of
#: a longer word" guard on both sides of a tag, mirroring the renderer's
#: (?<![\p{L}\p{N}]) ... (?![\p{L}\p{N}]).
_WORD = r"[^\W_]"

#: One slug segment list: the same shape the editor writes into a note.
_SLUG = r"[0-9a-z]+(?:-[0-9a-z]+)*"

_TAG = re.compile(rf"(?<!{_WORD})[@#]({_SLUG})(?!{_WORD})", re.IGNORECASE)

#: How many referenced pages reach the prompt at all.
MAX_REFERENCED_PAGES = 12

#: Bounds on the rendered content. The notes are the SOURCE of the generation,
#: so the context read out of the wiki must never crowd them out of the prompt:
#: the whole section is capped, and the caps per field only stop one enormous
#: page from eating the entire budget.
MAX_CONTEXT_CHARS = 12_000
MAX_FIELD_CHARS = 800
MAX_LIST_ITEMS = 15
MAX_ITEM_CHARS = 240


def slugify(title: str) -> str:
    """The slug a page with this title gets when it has none of its own.

    Mirrors slugify() in components/page-link-editor.tsx: the editor inserts the
    page's stored slug, and this derivation when it has none, so a reference to
    a slug-less page has to resolve here too. wiki-service derives the same
    shape when it creates a page.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return cleaned or "page"


def referenced_slugs(text: str) -> list[str]:
    """The page slugs a piece of note text references, in order, once each.

    Lowercased: slugs are stored lowercase, and a hand-typed "@Bree" means the
    same page as the "@bree" the editor would have inserted.
    """
    found: dict[str, None] = {}
    for match in _TAG.finditer(text or ""):
        found.setdefault(match.group(1).lower(), None)
    return list(found)


def page_keys(page: dict[str, Any]) -> list[str]:
    """Every slug that could name this page: its own, plus the one its title
    would get (a page created before slugs existed, or by another path)."""
    keys: list[str] = []
    slug = str(page.get("slug") or "").strip().lower()
    if slug:
        keys.append(slug)
    title = str(page.get("title") or "")
    derived = slugify(title) if title else ""
    if derived and derived not in keys:
        keys.append(derived)
    return keys


def resolve_references(
    notes: Iterable[dict[str, Any]],
    existing_pages: list[dict[str, Any]] | None,
    *,
    limit: int = MAX_REFERENCED_PAGES,
) -> list[dict[str, Any]]:
    """The campaign pages the notes reference, in the order first referenced.

    A slug that matches no page is dropped rather than reported: a note may well
    name a page that does not exist yet - that is how the DM proposes one - and
    there is no content to read out for it.
    """
    wanted: dict[str, None] = {}
    for note in notes or []:
        for slug in referenced_slugs(str(note.get("body") or "")):
            wanted.setdefault(slug, None)
    if not wanted:
        return []

    by_key: dict[str, dict[str, Any]] = {}
    for page in existing_pages or []:
        if str(page.get("status") or "") == "archived":
            continue
        for key in page_keys(page):
            by_key.setdefault(key, page)

    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slug in wanted:
        page = by_key.get(slug)
        if page is None:
            continue
        page_id = str(page.get("id") or "")
        if page_id in seen:
            continue
        seen.add(page_id)
        resolved.append(page)
        if len(resolved) >= limit:
            break
    return resolved


def _clip(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    """One line, whitespace collapsed, cut at a word-ish boundary."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [...]"


def _render_page(page: dict[str, Any]) -> str:
    """What one page currently says, as the model reads it.

    The field list is note_attributes.PROSE_FIELDS, the module's mirror of the
    wiki's own content_json shape: strings are printed as they are, lists one
    item per line. Anything else the page carries - the structured attributes
    above all - is printed as JSON, because that is what it is.
    """
    content = page.get("content_json") or {}
    if not isinstance(content, dict):
        content = {}
    title = str(page.get("title") or "?")
    kind = str(page.get("kind") or "page")
    slug = str(page.get("slug") or slugify(title))
    lines = [f"--- {title} ({kind}, slug {slug}) ---"]

    for field in PROSE_FIELDS:
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            lines.append(field + ": " + _clip(value))
        elif isinstance(value, list):
            items = [item for item in value if isinstance(item, str) and item.strip()]
            if not items:
                continue
            lines.append(field + ":")
            lines.extend("- " + _clip(item, MAX_ITEM_CHARS) for item in items[:MAX_LIST_ITEMS])
            if len(items) > MAX_LIST_ITEMS:
                lines.append("- (+" + str(len(items) - MAX_LIST_ITEMS) + " more)")

    attributes = content.get("attributes")
    if isinstance(attributes, dict) and attributes:
        lines.append("attributes: " + json.dumps(attributes, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def render_reference_context(pages: list[dict[str, Any]]) -> str:
    """The pages the notes tag, with what they already say.

    Bounded by MAX_CONTEXT_CHARS: whatever does not fit is reported as a count
    rather than silently dropped, so a proposal that ignored a tagged page can
    be told apart from a proposal that never saw it.
    """
    blocks: list[str] = []
    used = 0
    dropped = 0
    for index, page in enumerate(pages):
        block = _render_page(page)
        room = MAX_CONTEXT_CHARS - used
        if room <= 0:
            dropped = len(pages) - index
            break
        if len(block) > room:
            blocks.append(block[:room].rstrip() + " [...]")
            dropped = len(pages) - index - 1
            break
        blocks.append(block)
        used += len(block) + 2
    if dropped:
        blocks.append("(+" + str(dropped) + " more referenced page(s) not shown)")
    return "\n\n".join(blocks)

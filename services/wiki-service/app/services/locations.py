"""The campaign's locations as a tree.

A wiki page is stored flat: nothing in the wiki_pages table says that the
"Locanda del Fumo Aspro" sits inside "Fatumastra", which sits inside
"Luxastra". The containment is written in prose instead, in two places of
content_json:

- attributes.region - the place this one sits in ("Fatumastra"); the field
  every location type may carry (see app/page_attributes.py)
- attributes.notable_locations - the places a settlement/region lists as
  being inside it

Neither is a foreign key, so the tree is DERIVED here, once per request:

1. a page whose region names another location page (by title, slug or alias)
   becomes that page's child;
2. a page nobody claimed, but which a broader place lists in
   notable_locations, hangs under that place;
3. everything else is a root - including a page whose region names no page at
   all, which is reported as unresolved_region so the UI can say why the place
   stayed at the top level;
4. an edge that would close a loop is dropped, so a tree always renders.

Name matching is case/accent/punctuation-insensitive ("Locanda del Fumo Aspro"
finds "locanda-del-fumo-aspro"), because the region string is free prose and
the slug is not.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WikiPage
from app.services.pages import PUBLIC, PUBLISHED

#: the only page kind this module knows about
LOCATION_KIND = "location"

#: Coarse -> fine ordering of the location types. Used to sort siblings (a
#: world reads before the city inside it) and to break ties when two pages
#: answer to the same name: the broader place wins the children.
TYPE_RANK: dict[str, int] = {
    "world": 0,
    "continent": 1,
    "region": 2,
    "city": 3,
    "town": 3,
    "village": 3,
    "wilderness": 4,
    "structure": 5,
    "building": 6,
    "dungeon": 6,
    "other": 7,
}

DEFAULT_TYPE = "other"

#: Everything that is not a letter or a digit collapses to a single space, so
#: "Locanda del Fumo Aspro" and "locanda-del-fumo-aspro" share one key.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


@dataclass
class LocationNode:
    """One place of the tree: the page plus the places it contains."""

    page: WikiPage
    location_type: str
    #: The free-text region of this page when it named no page (None when the
    #: page has no region, or the region resolved to a parent).
    unresolved_region: str | None = None
    children: list[LocationNode] = field(default_factory=list)


# ------------------------------------------------------------------ helpers


def _attributes(page: WikiPage) -> dict:
    """The attributes object of a page's content_json (never None)."""
    content = page.content_json if isinstance(page.content_json, dict) else {}
    attributes = content.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _text_list(value: object) -> list[str]:
    """The non-empty strings of a content_json list field."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _match_key(value: str) -> str:
    """Case/accent/punctuation-insensitive key for matching place names."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_ish = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", ascii_ish.lower()).strip()


def location_type_of(page: WikiPage) -> str:
    """The page's location_type, defaulting to 'other' for anything unknown."""
    value = _attributes(page).get("location_type")
    return value if isinstance(value, str) and value in TYPE_RANK else DEFAULT_TYPE


def _names_of(page: WikiPage) -> list[str]:
    """Every name this page answers to: title, slug and aliases."""
    content = page.content_json if isinstance(page.content_json, dict) else {}
    return [page.title, page.slug, *_text_list(content.get("aliases"))]


def _sort_key(node: LocationNode) -> tuple[int, str]:
    return (TYPE_RANK.get(node.location_type, TYPE_RANK[DEFAULT_TYPE]), node.page.title.lower())


# ------------------------------------------------------------------- build


def build_location_tree(pages: list[WikiPage]) -> list[LocationNode]:
    """Derive the containment tree of a set of location pages.

    Pure function over already-loaded pages: the caller decides which pages
    the role may see, this decides how they nest.
    """
    nodes = {
        page.id: LocationNode(
            page=page,
            location_type=location_type_of(page),
            unresolved_region=None,
        )
        for page in pages
    }

    # name key -> the pages answering to it, broadest first
    by_name: dict[str, list[WikiPage]] = {}
    for page in pages:
        for name in _names_of(page):
            key = _match_key(name)
            if key:
                by_name.setdefault(key, []).append(page)
    for candidates in by_name.values():
        candidates.sort(key=lambda p: (TYPE_RANK[location_type_of(p)], p.title.lower()))

    def claim(name: str, child: WikiPage) -> WikiPage | None:
        """The page a name refers to, when it is not the child itself."""
        for candidate in by_name.get(_match_key(name), []):
            if candidate.id != child.id:
                return candidate
        return None

    parent: dict[UUID, WikiPage] = {}

    # 1) the explicit parent: "I sit in <region>"
    for page in pages:
        region = _attributes(page).get("region")
        if not isinstance(region, str) or not region.strip():
            continue
        found = claim(region, page)
        if found is None:
            nodes[page.id].unresolved_region = region.strip()
        else:
            parent[page.id] = found

    # 2) the reverse declaration: "these places are inside me". Only pages no
    #    region claimed take part, so an explicit parent always wins.
    for page in pages:
        for name in _text_list(_attributes(page).get("notable_locations")):
            for candidate in by_name.get(_match_key(name), []):
                if candidate.id != page.id and candidate.id not in parent:
                    parent[candidate.id] = page

    _drop_cycles(pages, parent)

    roots: list[LocationNode] = []
    for page in pages:
        holder = parent.get(page.id)
        if holder is None:
            roots.append(nodes[page.id])
        else:
            nodes[holder.id].children.append(nodes[page.id])
    _sort_nodes(roots)
    return roots


def _drop_cycles(pages: list[WikiPage], parent: dict[UUID, WikiPage]) -> None:
    """Cut the edges that would close a loop ("A is in B is in A").

    Region strings are prose and nothing stops two pages from naming each
    other; a loop would make the render recurse forever, so the page that
    closes it is promoted to a root instead.
    """
    for page in pages:
        seen: set[UUID] = set()
        current = page
        while current.id in parent:
            if current.id in seen:
                parent.pop(current.id, None)
                break
            seen.add(current.id)
            current = parent[current.id]


def _sort_nodes(nodes: list[LocationNode]) -> None:
    nodes.sort(key=_sort_key)
    for node in nodes:
        _sort_nodes(node.children)


# ------------------------------------------------------------------ queries


async def location_tree(db: AsyncSession, campaign_id: UUID, role: str) -> list[LocationNode]:
    """The location pages of a campaign the caller may see, nested.

    Players get the same visibility filter as the flat list (published AND
    public): a page they cannot read cannot hold one they can, so its children
    surface as roots instead of vanishing.
    """
    stmt = select(WikiPage).where(
        WikiPage.campaign_id == campaign_id, WikiPage.kind == LOCATION_KIND
    )
    if role != "dm":
        stmt = stmt.where(WikiPage.status == PUBLISHED, WikiPage.visibility == PUBLIC)
    pages = list((await db.execute(stmt)).scalars().all())
    return build_location_tree(pages)


def count_nodes(nodes: list[LocationNode]) -> int:
    """Total number of places in a tree (every node, at every depth)."""
    return sum(1 + count_nodes(node.children) for node in nodes)

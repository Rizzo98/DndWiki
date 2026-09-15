"""The internal HTTP clients: the routes they call and what they keep.

These are contract tests. Every one of them fails against a service whose API
has moved, which is the point: a client that calls a route nobody serves is
silent at import time and only shows up as a 404 in a worker log - exactly how
the wiki character route shipped broken.
"""

import asyncio

from app.clients.wiki_service import WikiServiceClient


class _Stub(WikiServiceClient):
    """A WikiServiceClient that records the request instead of making it."""

    def __init__(self, payload):
        self.calls = []
        self._payload = payload

    async def request(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, params))
        return self._payload


def _page(**overrides):
    page = {
        "id": "p1",
        "title": "Thorin",
        "kind": "character",
        "status": "draft",
        "content_json": {"attributes": {"class": "Fighter"}},
    }
    page.update(overrides)
    return page


def test_character_pages_calls_the_route_wiki_service_actually_serves():
    """GET /internal/wiki/pages is the flat listing content-service also uses.

    The old call went to /internal/campaigns/{id}/characters, which no service
    has ever served.
    """
    stub = _Stub([_page()])
    pages = asyncio.run(stub.character_pages("camp-1"))
    assert stub.calls == [("GET", "/internal/wiki/pages", {"campaign_id": "camp-1", "limit": 500})]
    assert pages == [_page()]


def test_only_character_pages_survive():
    """A campaign's pages include locations, items and events; the capability
    channel wants the cast and nothing else."""
    stub = _Stub(
        [
            _page(),
            _page(id="p2", title="Neverwinter", kind="location"),
            _page(id="p3", title="Bag of Holding", kind="item"),
        ]
    )
    pages = asyncio.run(stub.character_pages("camp-1"))
    assert [page["title"] for page in pages] == ["Thorin"]


def test_a_non_list_payload_is_tolerated():
    """An error body or a wrapped response must not crash the worker."""
    stub = _Stub({"detail": "nope"})
    assert asyncio.run(stub.character_pages("camp-1")) == []


def test_a_page_without_a_kind_is_not_a_character():
    stub = _Stub([_page(kind=None), _page(id="p2", kind="character")])
    pages = asyncio.run(stub.character_pages("camp-1"))
    assert [page["id"] for page in pages] == ["p2"]

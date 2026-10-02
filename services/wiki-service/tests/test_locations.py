"""Location hierarchy: deriving the containment tree from free-text fields."""

import uuid

from app import services
from app.models import WikiPage
from app.services.locations import build_location_tree, count_nodes, location_type_of

CAMPAIGN = uuid.UUID("56eee5fa-c71b-49c1-a009-0c48c56404b1")


def _page(title, *, attributes=None, aliases=None, slug=None, id=None) -> WikiPage:
    """A detached location page (no DB): the builder only reads fields."""
    content: dict = {}
    if attributes is not None:
        content["attributes"] = attributes
    if aliases is not None:
        content["aliases"] = aliases
    return WikiPage(
        id=id or uuid.uuid4(),
        campaign_id=CAMPAIGN,
        kind="location",
        title=title,
        slug=slug or title.lower().replace(" ", "-"),
        content_json=content,
        status="published",
        visibility="public",
    )


def _shape(nodes):
    """The tree as nested (title, children) tuples, for readable asserts."""
    return [(node.page.title, _shape(node.children)) for node in nodes]


def _find(nodes, title):
    for node in nodes:
        if node.page.title == title:
            return node
        found = _find(node.children, title)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------- build_location_tree


def test_region_nests_place_inside_place():
    """The campaign example: world > city > tavern."""
    luxastra = _page("Luxastra", attributes={"location_type": "world"})
    fatumastra = _page(
        "Fatumastra",
        attributes={"location_type": "city", "region": "Luxastra"},
    )
    locanda = _page(
        "Locanda del Fumo Aspro",
        attributes={"location_type": "building", "region": "Fatumastra"},
    )

    roots = build_location_tree([locanda, fatumastra, luxastra])

    assert _shape(roots) == [
        (
            "Luxastra",
            [("Fatumastra", [("Locanda del Fumo Aspro", [])])],
        )
    ]
    assert count_nodes(roots) == 3


def test_region_matches_slug_and_alias_not_only_title():
    """A region is prose: it may name the slug, or a name only the alias knows."""
    parent = _page(
        "Concaverde", attributes={"location_type": "region"}, aliases=["Coca Verde"]
    )
    by_slug = _page("Palude Nera", attributes={"location_type": "wilderness", "region": "concaverde"})
    by_alias = _page("Bosco Antico", attributes={"location_type": "wilderness", "region": "Coca Verde"})

    roots = build_location_tree([parent, by_slug, by_alias])

    assert _shape(roots) == [
        ("Concaverde", [("Bosco Antico", []), ("Palude Nera", [])]),
    ]


def test_matching_ignores_case_accents_and_punctuation():
    parent = _page("Città di Fatumastra", attributes={"location_type": "city"})
    child = _page("Ospedale", attributes={"location_type": "building", "region": "citta di fatumastra"})

    roots = build_location_tree([parent, child])

    assert _shape(roots) == [("Città di Fatumastra", [("Ospedale", [])])]


def test_region_naming_no_page_leaves_a_root_with_the_hint():
    orphan = _page("Torre Solitaria", attributes={"location_type": "structure", "region": "Terra Ignota"})

    roots = build_location_tree([orphan])

    assert _shape(roots) == [("Torre Solitaria", [])]
    assert roots[0].unresolved_region == "Terra Ignota"


def test_region_naming_itself_is_not_a_parent():
    page = _page("Fatumastra", attributes={"location_type": "city", "region": "Fatumastra"})

    roots = build_location_tree([page])

    assert _shape(roots) == [("Fatumastra", [])]
    assert roots[0].unresolved_region == "Fatumastra"


def test_notable_locations_claims_a_page_without_a_region():
    city = _page(
        "Fatumastra",
        attributes={
            "location_type": "city",
            "notable_locations": ["Piazza principale", "Strada Maestra"],
        },
    )
    piazza = _page("Piazza principale", attributes={"location_type": "structure"})
    strada = _page("Strada Maestra", attributes={"location_type": "structure"})

    roots = build_location_tree([city, piazza, strada])

    assert _shape(roots) == [("Fatumastra", [("Piazza principale", []), ("Strada Maestra", [])])]


def test_explicit_region_wins_over_a_notable_locations_claim():
    outer = _page(
        "Luxastra",
        attributes={"location_type": "world", "notable_locations": ["Fatumastra"]},
    )
    other_region = _page("Concaverde", attributes={"location_type": "region"})
    city = _page(
        "Fatumastra",
        attributes={"location_type": "city", "region": "Concaverde"},
    )

    roots = build_location_tree([outer, other_region, city])

    # the world still sorts above the region, but Fatumastra sits in Concaverde
    assert _shape(roots) == [
        ("Luxastra", []),
        ("Concaverde", [("Fatumastra", [])]),
    ]


def test_mutual_regions_do_not_loop():
    a = _page("Alfa", attributes={"location_type": "city", "region": "Beta"})
    b = _page("Beta", attributes={"location_type": "city", "region": "Alfa"})

    roots = build_location_tree([a, b])

    # one edge survives, the one closing the loop is cut
    assert count_nodes(roots) == 2
    assert _find(roots, "Alfa") is not None and _find(roots, "Beta") is not None
    for node in roots:
        assert node.children == [] or node.children[0].children == []


def test_siblings_sort_coarse_to_fine_then_by_title():
    world = _page("Luxastra", attributes={"location_type": "world"})
    city = _page("Fatumastra", attributes={"location_type": "city", "region": "Luxastra"})
    region = _page("Concaverde", attributes={"location_type": "region", "region": "Luxastra"})
    building = _page("Locanda", attributes={"location_type": "building", "region": "Luxastra"})

    roots = build_location_tree([world, city, region, building])

    assert _shape(roots) == [
        ("Luxastra", [("Concaverde", []), ("Fatumastra", []), ("Locanda", [])]),
    ]


def test_unknown_location_type_reads_as_other():
    page = _page("Posto Strano", attributes={"location_type": "castle"})

    assert location_type_of(page) == "other"


# ------------------------------------------------------------- API endpoint


async def test_endpoint_returns_the_campaign_tree(client, fake_campaign, claims, user_id):
    campaign = str(CAMPAIGN)
    fake_campaign.roles[(CAMPAIGN, user_id)] = "dm"
    claims["sub"] = str(user_id)

    for title, attributes in [
        ("Luxastra", {"location_type": "world"}),
        ("Fatumastra", {"location_type": "city", "region": "Luxastra"}),
        ("Locanda del Fumo Aspro", {"location_type": "building", "region": "Fatumastra"}),
    ]:
        response = await client.post(
            "/api/wiki/pages",
            json={
                "campaign_id": campaign,
                "kind": "location",
                "title": title,
                "content_json": {"attributes": attributes},
                "status": "published",
            },
        )
        assert response.status_code == 201, response.text

    response = await client.get(f"/api/wiki/campaigns/{campaign}/locations/tree")

    assert response.status_code == 200, response.text
    tree = response.json()
    assert [node["title"] for node in tree] == ["Luxastra"]
    city = tree[0]["children"][0]
    assert city["title"] == "Fatumastra"
    assert city["location_type"] == "city"
    assert city["children"][0]["title"] == "Locanda del Fumo Aspro"
    # summary fields survive the nesting (the row renders them)
    assert city["slug"] == "fatumastra"
    assert city["status"] == "published"


async def test_endpoint_hides_dm_only_branch_from_players(session_factory, client, fake_campaign, claims, user_id):
    """A page a player cannot read cannot hold one they can: the child floats."""
    async with session_factory() as db:
        await services.create_page(
            db,
            campaign_id=CAMPAIGN,
            kind="location",
            title="Luxastra",
            content_json={"attributes": {"location_type": "world"}},
            status="published",
        )
        await services.create_page(
            db,
            campaign_id=CAMPAIGN,
            kind="location",
            title="Covo Segreto",
            content_json={"attributes": {"location_type": "dungeon", "region": "Luxastra"}},
            status="published",
            visibility="dm_only",
        )

    fake_campaign.roles[(CAMPAIGN, user_id)] = "player"
    claims["sub"] = str(user_id)

    response = await client.get(f"/api/wiki/campaigns/{CAMPAIGN}/locations/tree")

    assert response.status_code == 200, response.text
    assert [node["title"] for node in response.json()] == ["Luxastra"]


async def test_players_see_only_published_public_locations(session_factory, client, fake_campaign, claims, user_id):
    async with session_factory() as db:
        await services.create_page(
            db,
            campaign_id=CAMPAIGN,
            kind="location",
            title="Bozza",
            content_json={"attributes": {"location_type": "city"}},
            status="draft",
        )

    fake_campaign.roles[(CAMPAIGN, user_id)] = "player"
    claims["sub"] = str(user_id)

    response = await client.get(f"/api/wiki/campaigns/{CAMPAIGN}/locations/tree")

    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_endpoint_requires_membership(client):
    response = await client.get(f"/api/wiki/campaigns/{CAMPAIGN}/locations/tree")

    assert response.status_code == 403


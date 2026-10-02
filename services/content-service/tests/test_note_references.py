"""Tests for the pages a note TAGS with '@slug' (app/note_references.py).

A tag is the one place in a note where the DM says "this passage is about THAT
page", so it is the one place the pipeline does not have to guess an identity
from a name that happens to look alike. These tests pin down what counts as a
tag, which page a tag resolves to, and what of that page's content is read out
to the model when it reads the notes.
"""

from app.note_references import (
    MAX_CONTEXT_CHARS,
    MAX_LIST_ITEMS,
    page_keys,
    referenced_slugs,
    render_reference_context,
    resolve_references,
    slugify,
)


def _page(**over):
    page = {
        "id": "p1",
        "title": "Bree",
        "slug": "bree",
        "kind": "location",
        "status": "published",
        "content_json": {"summary": "A market town on the north road."},
    }
    page.update(over)
    return page


def _note(body: str, title: str = "Note") -> dict:
    return {"title": title, "body": body}


# ---------------------------------------------------------------- the grammar


def test_both_markers_are_read():
    # The wiki's own fields write '#', the toolkit's editor writes '@'; the
    # renderer treats the two as one token, so the pipeline reads both.
    assert referenced_slugs("the party reaches @bree and #bree") == ["bree"]


def test_slugs_keep_their_hyphens_and_lose_their_case():
    assert referenced_slugs("see @locanda-del-fumo-aspro and @Bree") == [
        "locanda-del-fumo-aspro",
        "bree",
    ]


def test_each_slug_is_reported_once_in_the_order_referenced():
    assert referenced_slugs("@bree then @kaelor then @bree") == ["bree", "kaelor"]


def test_a_token_glued_to_a_word_is_not_a_reference():
    # An e-mail address must never tag anything. This is the rule the renderer
    # applies too, so what is not linked here is not linked on the page either.
    assert referenced_slugs("write to dm@bree.example") == []
    assert referenced_slugs("the x@bree run") == []


def test_a_word_after_the_slug_is_not_part_of_it():
    assert referenced_slugs("@bree's market") == ["bree"]


def test_prose_without_tags_references_nothing():
    assert referenced_slugs("The party rides north at dawn.") == []
    assert referenced_slugs("") == []


# ------------------------------------------------------------ slugify mirror


def test_slugify_matches_what_the_editor_would_have_written():
    # components/page-link-editor.tsx inserts the page's stored slug, and this
    # derivation when the page has none - so a reference to a slug-less page has
    # to resolve here as well.
    assert slugify("Locanda del Fumo Aspro") == "locanda-del-fumo-aspro"
    assert slugify("  Bree  ") == "bree"
    assert slugify("!!!") == "page"


def test_a_page_is_findable_by_its_slug_and_by_its_title():
    assert page_keys(_page()) == ["bree"]
    assert page_keys(_page(slug=None, title="Locanda del Fumo")) == ["locanda-del-fumo"]
    assert page_keys(_page(slug="bree-town", title="Bree")) == ["bree-town", "bree"]


# ----------------------------------------------------------------- resolving


def test_a_tag_resolves_to_the_page_that_owns_the_slug():
    pages = [_page(), _page(id="p2", title="Kaelor", slug="kaelor")]
    resolved = resolve_references([_note("@kaelor rides for @bree")], pages)
    assert [page["id"] for page in resolved] == ["p2", "p1"]


def test_a_page_without_a_slug_resolves_by_its_title():
    pages = [_page(slug=None, title="Locanda del Fumo Aspro")]
    resolved = resolve_references([_note("we meet at @locanda-del-fumo-aspro")], pages)
    assert [page["title"] for page in resolved] == ["Locanda del Fumo Aspro"]


def test_an_unknown_tag_resolves_to_nothing():
    # A note may name a page that does not exist yet - that is how the DM
    # proposes one - and there is no content to read out for it.
    assert resolve_references([_note("the road to @nowhere")], [_page()]) == []


def test_an_archived_page_is_not_resolved():
    assert resolve_references([_note("@bree")], [_page(status="archived")]) == []


def test_a_page_tagged_twice_is_read_out_once():
    notes = [_note("first @bree"), _note("again @bree", "Second note")]
    assert len(resolve_references(notes, [_page()])) == 1


def test_a_tag_in_any_of_the_notes_counts():
    notes = [_note("nothing here"), _note("@bree it is", "Second note")]
    assert len(resolve_references(notes, [_page()])) == 1


def test_only_the_first_tagged_pages_are_read_out():
    pages = [_page(id="p" + str(i), slug="page-" + str(i), title="Page " + str(i)) for i in range(5)]
    notes = [_note(" ".join("@" + page["slug"] for page in pages))]
    assert len(resolve_references(notes, pages, limit=2)) == 2


# ----------------------------------------------------------------- rendering


def test_the_pages_own_content_is_read_out():
    pages = [
        _page(
            content_json={
                "summary": "A market town on the north road.",
                "history": "Burnt once, rebuilt twice.",
                "facts": ["The inn is called the Prancing Pony."],
                "aliases": ["Bree-land"],
                "attributes": {"location_type": "town", "population": 1200},
            }
        )
    ]
    text = render_reference_context(pages)
    assert "--- Bree (location, slug bree) ---" in text
    assert "summary: A market town on the north road." in text
    assert "history: Burnt once, rebuilt twice." in text
    assert "- The inn is called the Prancing Pony." in text
    assert "- Bree-land" in text
    assert '"location_type": "town"' in text


def test_a_page_with_nothing_written_is_still_named():
    assert render_reference_context([_page(content_json={})]).strip() == (
        "--- Bree (location, slug bree) ---"
    )


def test_a_long_field_is_cut_and_says_so():
    text = render_reference_context([_page(content_json={"summary": "x" * 5000})])
    assert len(text) < 2000
    assert "[...]" in text


def test_a_long_list_is_cut_and_says_how_much_is_missing():
    facts = ["fact " + str(i) for i in range(MAX_LIST_ITEMS + 4)]
    text = render_reference_context([_page(content_json={"facts": facts})])
    assert "fact " + str(MAX_LIST_ITEMS - 1) in text
    assert "fact " + str(MAX_LIST_ITEMS) + "\n" not in text
    assert "(+4 more)" in text


def test_the_section_is_bounded_so_the_notes_keep_their_room():
    # The notes are the SOURCE of the generation: content read out of the wiki
    # must not push them out of the prompt, so the whole section is capped and
    # says how many pages it left out.
    pages = [
        _page(id="p" + str(i), title="Page " + str(i), content_json={"summary": "y" * 4000})
        for i in range(20)
    ]
    text = render_reference_context(pages)
    assert len(text) <= MAX_CONTEXT_CHARS + 200
    assert "more referenced page(s) not shown" in text

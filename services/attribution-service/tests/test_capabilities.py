"""Capability store tests: who can do what, and when that may veto."""


from app.capabilities import (
    Capability,
    CapabilityStore,
    from_answer,
    from_dm_sheet,
    from_wiki_characters,
    is_well_formed,
    normalize,
    normalize_all,
)

A, B = "m-a", "m-b"


def _page(title, attributes, aliases=None):
    """A wiki character page in the shape GET /internal/wiki/pages returns."""
    return {
        "id": "page-1",
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "kind": "character",
        "status": "draft",
        "aliases": aliases or [],
        "content_json": {"attributes": attributes},
    }


def test_requirements_must_be_kind_colon_name():
    assert is_well_formed("spell:fireball") is True
    assert is_well_formed("Spell:Fireball") is True
    assert is_well_formed("fireball") is False
    assert is_well_formed("nonsense:thing") is False
    assert is_well_formed("spell:") is False


def test_normalization_lowercases_both_halves():
    assert normalize("Spell:Fire Ball") == "spell:fire ball"
    assert normalize_all(["spell:Fireball", "junk", "spell:fireball"]) == ["spell:fireball"]


def test_a_dm_sheet_is_authoritative_and_a_wiki_guess_is_not():
    store = CapabilityStore()
    for capability in from_wiki_characters(
        [_page("Alaric", {"class": "Wizard"})], member_of={"alaric": A}
    ):
        store.add(capability)
    assert store.authoritative is False
    assert store.compat(A, "spell:fireball") == 0.0  # unknown, NOT a veto

    sheet = CapabilityStore()
    for capability in from_dm_sheet(
        {A: ["class:wizard", "spell:fireball"], B: ["class:fighter"]}
    ):
        sheet.add(capability)
    assert sheet.authoritative is True
    assert sheet.compat(A, "class:wizard") == 1.0
    # B IS on the sheet and lacks the requirement, so B is provably incapable -
    # that is the one condition that may veto
    assert sheet.compat(B, "class:wizard") == -1.0


def test_a_member_absent_from_the_sheet_is_unknown_not_incapable():
    """A member nobody filled in might be able to do anything. "We were not
    told" is not "no", and only a PROVABLE inability may veto (S16)."""
    store = CapabilityStore()
    for capability in from_dm_sheet({A: ["class:wizard"]}):
        store.add(capability)
    assert store.compat("m-nobody", "class:wizard") == 0.0


def test_a_stronger_source_is_never_overwritten_by_a_weaker_one():
    store = CapabilityStore()
    key = (A, "spell:fireball", "can")
    store.add(Capability(member_id=A, capability="spell:fireball", source="dm", confidence=1.0))
    store.add(Capability(member_id=A, capability="spell:fireball", source="transcript", confidence=0.4))
    assert store.entries[key].source == "dm"
    # ... but a more confident reading of the SAME source may refine it
    store.add(Capability(member_id=A, capability="spell:fireball", source="dm", confidence=1.0))
    assert store.entries[key].confidence == 1.0


def test_two_members_with_the_same_class_both_keep_it():
    """The store is keyed by (member, capability), not by capability.

    A party with two fighters is not exotic. Keying on the capability alone let
    the second one silently overwrite the first, so one of them looked like
    someone the engine knew nothing about.
    """
    store = CapabilityStore()
    store.add(Capability(member_id=A, capability="class:fighter", source="dm"))
    store.add(Capability(member_id=B, capability="class:fighter", source="dm"))
    assert store.of(A) == frozenset({"class:fighter"})
    assert store.of(B) == frozenset({"class:fighter"})
    assert store.compat(B, "class:fighter") == 1.0


def test_by_member_groups_only_positive_capabilities():
    store = CapabilityStore()
    store.add(Capability(member_id=A, capability="class:wizard", source="dm"))
    store.add(Capability(member_id=B, capability="feature:rage", source="wiki"))
    store.add(Capability(member_id=B, capability="spell:fireball", polarity="cannot", source="dm"))
    grouped = store.by_member()
    assert grouped[A] == frozenset({"class:wizard"})
    assert grouped[B] == frozenset({"feature:rage"})


def test_capabilities_learned_from_an_answer_are_confident():
    learned = from_answer(member_id=A, requirements=["Spell:Fireball"], session_id="s1")
    assert learned[0].source == "answer"
    assert learned[0].confidence == 0.95
    assert learned[0].capability == "spell:fireball"


def test_wiki_mining_reads_the_class_off_a_real_character_page():
    """The shape is what wiki-service returns, not a bespoke 'character' object.

    The page records the class and nothing else capability-shaped, so that is
    what comes out. Inventing abilities from the page text would put a guess
    into the one channel whose whole job is to be checkable.
    """
    capabilities = from_wiki_characters(
        [
            _page("Sneaky Pete", {"character_type": "player", "class": "Rogue", "race": "Halfling"}),
            _page("Thorin", {"character_type": "player", "class": "Fighter"}),
        ],
        member_of={"sneaky pete": A, "thorin": B},
    )
    assert {c.capability for c in capabilities} == {"class:rogue", "class:fighter"}
    assert {c.member_id for c in capabilities} == {A, B}
    assert all(c.source == "wiki" for c in capabilities)


def test_a_page_nobody_at_the_table_owns_is_skipped():
    """A character page with no roster match is information about nobody.

    Guessing which member an unmatched page belongs to would put a wrong class
    on a real person, which is worse than having no capability at all.
    """
    assert from_wiki_characters([_page("A Stranger", {"class": "Wizard"})]) == []
    assert from_wiki_characters(
        [_page("A Stranger", {"class": "Wizard"})], member_of={"thorin": A}
    ) == []


def test_a_page_titled_after_an_alias_still_resolves():
    page = _page("Pete", {"class": "Rogue"})
    page["aliases"] = ["Sneaky Pete"]
    capabilities = from_wiki_characters([page], member_of={"sneaky pete": A})
    assert [c.member_id for c in capabilities] == [A]


def test_payload_is_json_friendly():
    store = CapabilityStore()
    store.add(Capability(member_id=A, capability="class:wizard", source="dm"))
    payload = store.as_payload()
    assert payload["authoritative"] is True
    assert payload["members"] == {A: ["class:wizard"]}

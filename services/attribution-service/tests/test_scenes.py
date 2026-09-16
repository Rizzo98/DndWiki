"""Scene reading tests: where the session happens, and who is there.

The engine acts on exactly one thing from this reading - who the record places
SOMEWHERE ELSE - so the tests are about that claim and about the reading never
producing evidence it cannot support.
"""

from app.scenes import (
    Scene,
    SceneMap,
    merge_scene_parts,
    parse_scenes,
    render_moment_view,
)

ORDINALS = {f"u_{index:05d}": index for index in range(1, 11)}
NAMES = {"dalia drif": "Dalia Drif", "shiran konno": "Shiran Konno", "hann caleto": "Hann Caleto"}


def raw(*scenes):
    return {"scenes": list(scenes)}


def test_a_reading_covers_every_moment_including_the_ends():
    """"Who is here" is per MOMENT, so a gap in the reading would be a moment
    with no place at all - and a moment with no place has no absence evidence."""
    scenes = parse_scenes(
        raw(
            {
                "location": "the tavern",
                "first": "u_00003",
                "last": "u_00006",
                "present": ["Dalia Drif"],
                "reason": "start",
            }
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    assert [scene.start_ordinal for scene in scenes] == [1]
    assert scenes[0].end_ordinal == 10
    assert scenes[0].location == "the tavern"


def test_stretches_are_contiguous_and_in_order():
    scenes = parse_scenes(
        raw(
            {"location": "a", "first": "u_00001", "last": "u_00004", "present": ["Dalia Drif"]},
            {"location": "b", "first": "u_00005", "last": "u_00007", "present": ["Shiran Konno"]},
            {"location": "c", "first": "u_00008", "last": "u_00010", "present": ["Hann Caleto"]},
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    assert [(s.start_ordinal, s.end_ordinal) for s in scenes] == [
        (1, 4),
        (5, 7),
        (8, 10),
    ]


def test_a_character_nobody_has_heard_of_is_dropped():
    """The engine keys candidates by member: a name that matches nobody is worse
    than no name, because it would be an exclusion that excludes nothing and a
    presence nobody can resolve."""
    scenes = parse_scenes(
        raw(
            {
                "location": "the tavern",
                "first": "u_00001",
                "last": "u_00010",
                "present": ["Dalia Drif", "Gandalf", "Shiran Konno"],
                "absent": ["Bilbo"],
            }
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    assert scenes[0].present == ("Dalia Drif", "Shiran Konno")
    assert scenes[0].absent == ()


def test_an_unknown_reference_drops_the_stretch_rather_than_the_session():
    scenes = parse_scenes(
        raw(
            {"location": "nowhere", "first": "u_09999", "last": "u_09999"},
            {"location": "the tavern", "first": "u_00001", "last": "u_00010"},
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    assert len(scenes) == 1
    assert scenes[0].location == "the tavern"


def test_nothing_usable_means_no_scenes_at_all():
    assert parse_scenes({}, ordinals=ORDINALS, roster_names=NAMES) == ()
    assert parse_scenes(raw(), ordinals=ORDINALS, roster_names=NAMES) == ()
    assert parse_scenes("nonsense", ordinals=ORDINALS, roster_names=NAMES) == ()


def test_the_lookup_answers_for_every_moment():
    scenes = parse_scenes(
        raw(
            {"location": "a", "first": "u_00001", "last": "u_00005"},
            {"location": "b", "first": "u_00006", "last": "u_00010"},
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    scene_map = SceneMap.build(scenes)
    assert scene_map.for_ordinal(1).location == "a"
    assert scene_map.for_ordinal(5).location == "a"
    assert scene_map.for_ordinal(6).location == "b"
    assert scene_map.for_ordinal(99).location == "b"


def test_a_session_read_in_parts_does_not_invent_an_absence_at_the_seam():
    """The model cannot see the moments before its own part, so it cannot know
    who was already gone. The first stretch of every later part keeps its place
    and its cast and loses the only thing the engine acts on."""
    first = parse_scenes(
        raw({"location": "the road", "first": "u_00001", "last": "u_00005",
             "present": ["Dalia Drif"], "absent": ["Hann Caleto"], "reason": "start"}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    second = parse_scenes(
        raw({"location": "the crypt", "first": "u_00006", "last": "u_00010",
             "present": ["Dalia Drif"], "absent": ["Hann Caleto"], "reason": "they go down"}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    merged = merge_scene_parts([first, second])
    assert merged[0].absent == ("Hann Caleto",)
    assert merged[1].absent == ()
    assert merged[1].present == ("Dalia Drif",)


def test_the_same_place_with_a_different_cast_is_two_stretches():
    """This is the whole point of the pass: the party splitting up inside one
    place has to survive as two stretches, because the cast is what the engine
    uses. Joining them by location would hide it."""
    scenes = parse_scenes(
        raw(
            {"location": "the cart", "first": "u_00001", "last": "u_00005",
             "present": ["Dalia Drif", "Shiran Konno"], "absent": ["Hann Caleto"],
             "reason": "start"},
            {"location": "the cart", "first": "u_00006", "last": "u_00010",
             "present": ["Dalia Drif", "Shiran Konno", "Hann Caleto"], "absent": [],
             "reason": "Hann joins them at the cart"},
        ),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    merged = merge_scene_parts([scenes])
    assert len(merged) == 2
    assert merged[1].present == ("Dalia Drif", "Shiran Konno", "Hann Caleto")


def test_a_stretch_that_continues_across_a_seam_is_joined():
    """...but the same place with the SAME cast, where the later part did not
    claim an event started it, is one stretch that the seam cut in two."""
    first = parse_scenes(
        raw({"location": "the road", "first": "u_00001", "last": "u_00005",
             "present": ["Dalia Drif"], "reason": "start"}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    second = parse_scenes(
        raw({"location": "the road", "first": "u_00006", "last": "u_00010",
             "present": ["Dalia Drif"], "reason": ""}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    merged = merge_scene_parts([first, second])
    assert len(merged) == 1
    assert merged[0].moment_count == 10


def test_an_unclear_location_is_never_used_to_join_stretches():
    first = parse_scenes(
        raw({"location": "unclear", "first": "u_00001", "last": "u_00005", "reason": "start"}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    second = parse_scenes(
        raw({"location": "unclear", "first": "u_00006", "last": "u_00010", "reason": ""}),
        ordinals=ORDINALS,
        roster_names=NAMES,
    )
    assert len(merge_scene_parts([first, second])) == 2


def test_the_view_carries_the_words():
    class Moment:
        def __init__(self, ref, start, text):
            self.ref, self.start, self.text = ref, start, text

    lines = render_moment_view(
        [Moment("u_00001", 12.0, "Letho va verso la  locanda"), Moment("u_00002", 20.0, "")]
    )
    assert lines == ["[u_00001 00:00:12] Letho va verso la locanda"]


# --- the mask the engine actually applies ------------------------------------


def test_a_stated_absence_excludes_a_member_for_the_moments_it_covers():
    """The one thing the engine acts on. Measured on a real session: 228 of 404
    moments lose a candidate this way, 63% of the session's stakes."""
    from app.evidence import EvidencePass
    from app.pipeline import _excluded_for

    pass_ = EvidencePass(
        scenes=(
            Scene(
                index=1,
                start_ordinal=1,
                end_ordinal=3,
                first_ref="u_00001",
                last_ref="u_00003",
                location="the cart",
                present=("Dalia Drif", "Shiran Konno"),
                absent=("Hann Caleto",),
            ),
            Scene(
                index=2,
                start_ordinal=4,
                end_ordinal=6,
                first_ref="u_00004",
                last_ref="u_00006",
                location="the cart",
                present=("Dalia Drif", "Shiran Konno", "Hann Caleto"),
            ),
        )
    )
    ordinals = {f"u_{index:05d}": index for index in range(1, 7)}
    names = {"dalia drif": "member:dalia", "hann caleto": "member:hann"}

    assert _excluded_for(pass_, "u_00001", ordinals, names) == ("member:hann",)
    assert _excluded_for(pass_, "u_00003", ordinals, names) == ("member:hann",)
    # ...and the moment he walks back in is his again
    assert _excluded_for(pass_, "u_00004", ordinals, names) == ()


def test_a_stated_absence_is_ignored_when_the_name_matches_nobody():
    from app.evidence import EvidencePass
    from app.pipeline import _excluded_for

    pass_ = EvidencePass(
        scenes=(
            Scene(
                index=1,
                start_ordinal=1,
                end_ordinal=2,
                first_ref="u_00001",
                last_ref="u_00002",
                absent=("Gandalf",),
            ),
        )
    )
    ordinals = {"u_00001": 1, "u_00002": 2}
    assert _excluded_for(pass_, "u_00001", ordinals, {"dalia drif": "member:dalia"}) == ()


def test_no_reading_means_no_presence_evidence_at_all():
    """A session whose reading failed is the session this feature did not exist
    for - not a session where everybody is absent."""
    from app.evidence import EvidencePass
    from app.pipeline import _excluded_for

    pass_ = EvidencePass()
    assert _excluded_for(pass_, "u_00001", {"u_00001": 1}, {}) == ()


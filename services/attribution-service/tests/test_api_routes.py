"""The public route surface, asserted rather than assumed.

The DM's browser reaches these routes through the gateway, which publishes this
service under a PathPrefix. A route that does not carry that prefix is not a
404 the caller can diagnose: it is answered by whichever service DOES own the
path, with a bare "Not Found" that names nothing.
"""

import uuid
from types import SimpleNamespace

from app.api import review
from app.propagate import Belief, VoiceState


def _voice_session(posteriors, *, voice_answers=None, statuses=None):
    """Enough of a ReviewSession for _voice_guesses: a belief and an infer()."""
    from app.propagate import Node

    refs = tuple(posteriors)
    belief = Belief(
        candidates=["member:a", "member:b", "unknown"],
        nodes={
            ref: Node(ref=ref, voice_id="V1", stakes=0.5, seconds=60.0) for ref in refs
        },
        potentials={},
        voices={"V1": VoiceState(id="V1", refs=refs, posterior={"member:a": 0.5})},
        voice_answers=dict(voice_answers or {}),
    )
    verdicts = {
        ref: SimpleNamespace(status=(statuses or {}).get(ref, "auto_low"))
        for ref in refs
    }
    return SimpleNamespace(
        propagator=SimpleNamespace(
            belief=belief,
            infer=lambda _belief: SimpleNamespace(posteriors=posteriors),
            verdicts=lambda _belief, _result=None: verdicts,
        ),
        belief=lambda: belief,
    )


ROSTER_SNAPSHOT = {
    "roster": [
        {
            "member_id": "a",
            "player_name": "Alice",
            "character_name": "Aramil",
            "role": "player",
            "label": "Alice — Aramil",
        }
    ]
}


# --- the progress the DM is shown -------------------------------------------


def test_the_progress_never_predicts_how_many_questions_are_left():
    """The DM was shown "about 6 questions to finish", answered 8, and finished
    with 84% of what matters still unattributed - because the number came from a
    greedy simulation that assumes every answer lands the way the evidence
    points. A review's length is a function of the answers, so the panel shows
    the two facts that hold instead: answered, and how many one review asks.
    """
    ahead = review.plan_progress(
        SimpleNamespace(questions_planned=6, questions_asked=3), max_questions=8
    )
    # The stored plan is deliberately NOT in the payload: it cannot be shown
    # without inviting a countdown the engine cannot honour.
    assert ahead == {"answered": 3, "max_questions": 8, "budget_spent": False}


def test_the_progress_says_when_the_budget_is_spent_even_past_the_plan():
    """A run can answer MORE than the plan predicted - 8 against a plan of 6 -
    and the panel has to survive it without printing "8 of 6 answered"."""
    spent = review.plan_progress(
        SimpleNamespace(questions_planned=6, questions_asked=8), max_questions=8
    )
    assert spent == {"answered": 8, "max_questions": 8, "budget_spent": True}


# --- the stretches the panel shows ------------------------------------------


def test_a_stretch_payload_keeps_the_names_and_the_members_apart():
    """The panel shows the names the reading used; the engine acts on member ids.

    A reading can name somebody the roster does not know (it is the DM reading
    their own table who will notice), and that has to stay visible rather than
    being silently resolved to nobody.
    """
    row = SimpleNamespace(
        index=3,
        location="Dietro la locanda, presso il carro coperto",
        reason="Il gruppo si divide",
        start_ordinal=244,
        end_ordinal=404,
        start_sec=1512.5,
        end_sec=2344.06,
        first_ref="u_00244",
        last_ref="u_00404",
        present_names=["Dalia Drif", "Shiran Konno"],
        absent_names=["Letho Hyman Feulner", "Gandalf"],
        npcs=["Sir Lucius", "the monkeys"],
        present_member_ids=[uuid.UUID(int=1)],
        absent_member_ids=[uuid.UUID(int=2)],
        revision=1,
    )
    payload = review.scene_payload(row)
    assert payload["moments"] == 161
    assert payload["location"].startswith("Dietro la locanda")
    assert payload["present"] == ["Dalia Drif", "Shiran Konno"]
    assert payload["absent"] == ["Letho Hyman Feulner", "Gandalf"]
    assert payload["absent_member_ids"] == [str(uuid.UUID(int=2))]
    assert payload["start_sec"] == 1512.5


def test_no_run_is_a_plan_of_nothing_rather_than_a_crash():
    progress = review.plan_progress(None, max_questions=8)
    assert progress == {"answered": 0, "max_questions": 8, "budget_spent": False}


def test_the_review_payload_is_actually_returned():
    """The body of GET /review, reachable without a database.

    It is a function for this reason: the route body lost its `return` in an
    edit and the panel rendered nothing at all, because a route body is the one
    place in this service that no test could reach.
    """
    session = SimpleNamespace(
        status=lambda questions_planned=None: {
            "coverage": 0.158,
            "unresolved": 0.844,
            "buckets": {"auto_high": 54},
            "questions_asked": 8,
            "questions_planned": questions_planned,
            "finished": True,
        },
        _coverage=lambda: 0.1582,
        unresolved=lambda: 0.8436,
    )
    run = SimpleNamespace(
        status="complete",
        questions_planned=6,
        questions_asked=8,
        coverage_before=0.1410,
        coverage_after=0.1582,
    )
    stats = SimpleNamespace(stop_reason="finished", engine_version="attr-1")

    payload = review.review_payload(session, stats, run, max_questions=8)

    assert payload["coverage"] == 0.1582
    # The number the DM is shown is a LOWER BOUND here (8 answered against a
    # plan of 6), and the payload must not offer anything to compare it to.
    assert payload["plan"] == {
        "answered": 8,
        "max_questions": 8,
        "budget_spent": True,
    }
    assert "questions" not in payload["plan"]
    # ...while the stored estimate stays available as a diagnostic.
    assert payload["run"]["questions_planned"] == 6
    assert payload["run"]["coverage_before"] == 0.141


# --- what an answer writes back ---------------------------------------------


def test_the_answer_path_keeps_the_record_of_what_the_answer_moved():
    """The snapshot the DM's answer writes carries the FULL belief state.

    This function used to hold its own copy of the belief encoder, and the copy
    had already lost a key: 'propagated_refs', the record of which moments an
    answer moved. Losing it is not a missing detail - the next read falls back
    to "somebody answered something, so every moment is inferred", which puts
    the whole session on the stricter status bar (S7.1). The effect was measured
    on a real session: the DM answered eight questions and the coverage shown
    went from 14.10% to 12.41%, instead of rising to 15.82%.
    """
    from app.propagate import Node
    from app.services.review import decode_belief

    belief = Belief(
        candidates=["member:a"],
        nodes={"u1": Node(ref="u1", stakes=0.8, seconds=5.0)},
        potentials={"u1": {"member:a": 1.0}},
        answers={"u1": "member:a"},
        propagated_refs={"u2", "u3"},
    )
    session = SimpleNamespace(belief=lambda: belief, asked=[])
    snapshot = review._snapshot_with(session, {"roster": [{"member_id": "a"}]})

    assert snapshot["propagated_refs"] == ["u2", "u3"]
    assert decode_belief(snapshot).propagated_refs == {"u2", "u3"}
    # ...and the keys that describe the SESSION are carried over, not rebuilt:
    # dropping them once left every later read unable to name a voice.
    assert snapshot["roster"] == [{"member_id": "a"}]


def test_a_voice_carries_what_the_engine_makes_of_it():
    """"Voices we found" is the control that replaces the legacy per-label panel,
    so it has to say what the engine currently thinks - a handle and a duration
    gave the DM nothing to disagree with."""
    session = _voice_session(
        {"u_00001": {"member:a": 0.9, "unknown": 0.1}},
        statuses={"u_00001": "auto_high"},
    )
    guesses = review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT))
    assert guesses["V1"] == {
        "guess": "member:a",
        "guess_label": "Alice — Aramil",
        "guess_confidence": 1.0,
        "confirmed": None,
    }


def test_a_guess_the_engine_will_not_act_on_is_reported_as_worth_nothing():
    """The posterior can be 0.99 for a candidate while every utterance is
    auto_low - a lone prior is not a corroborating channel. Reporting 0.99 here
    would tell the DM something the wiki will never see."""
    session = _voice_session(
        {"u_00001": {"member:a": 0.99, "unknown": 0.01}},
        statuses={"u_00001": "auto_low"},
    )
    guesses = review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT))
    assert guesses["V1"]["guess"] == "member:a"
    assert guesses["V1"]["guess_confidence"] == 0.0


def test_the_confidence_is_the_SHARE_of_that_voices_speech_that_is_settled():
    session = _voice_session(
        {
            "u_00001": {"member:a": 0.9, "unknown": 0.1},
            "u_00002": {"member:b": 0.9, "unknown": 0.1},
        },
        statuses={"u_00001": "auto_high", "u_00002": "auto_low"},
    )
    guesses = review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT))
    assert guesses["V1"]["guess"] == "member:a"
    # both utterances are 60s, only one is confidently attributed
    assert guesses["V1"]["guess_confidence"] == 0.5


def test_a_voice_the_dm_placed_says_so():
    session = _voice_session(
        {"u_00001": {"member:a": 0.9, "unknown": 0.1}},
        voice_answers={"V1": "member:a"},
        statuses={"u_00001": "user_confirmed"},
    )
    guesses = review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT))
    assert guesses["V1"]["confirmed"] == "member:a"


def test_a_voice_that_is_not_a_party_member_is_named_as_such():
    session = _voice_session(
        {"u_00001": {"unknown": 0.95, "member:a": 0.05}},
        statuses={"u_00001": "auto_high"},
    )
    guesses = review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT))
    assert guesses["V1"]["guess_label"] == "Someone not in the campaign"


def test_a_voice_with_no_utterance_posteriors_is_simply_absent():
    """Absent, not zero-filled: a panel that invented a "0 % sure" guess would be
    asserting something the engine never said."""
    session = _voice_session({})
    assert review._voice_guesses(session, SimpleNamespace(belief=ROSTER_SNAPSHOT)) == {}


def test_every_review_route_requires_an_authenticated_member():
    """The review API reads transcripts and writes attributions.

    These routes shipped with NO dependency at all: /api/attribution/config
    answered a request carrying the literal token "Bearer x" with 200, while
    every other service rejected the same token. Authentication is not the
    gateway's job here.
    """
    declared = {
        getattr(dependency.dependency, "__name__", str(dependency))
        for dependency in review.router.dependencies
    }
    assert "require_session_member" in declared, declared

#: docker-compose.yml: traefik.http.routers.attribution.rule PathPrefix(...).
GATEWAY_PREFIX = "/api/attribution"


def test_the_review_router_is_published_under_the_gateway_prefix():
    assert review.router.prefix.startswith(GATEWAY_PREFIX)


def test_every_review_route_hangs_off_the_prefix():
    paths = [route.path for route in review.router.routes]
    assert paths, "the review router has no routes at all"
    for path in paths:
        assert path.startswith(GATEWAY_PREFIX), path


def test_the_review_routes_are_the_ones_the_web_client_calls():
    """A hand-written list on purpose: it is the contract with apps/web.

    If a route is renamed here, apps/web/lib/api.ts must change in the same
    commit - and this test is what says so.
    """
    paths = {route.path for route in review.router.routes}
    for suffix in (
        "/review",
        "/review/next-question",
        "/review/answer",
        "/review/skip",
        "/review/finish",
        "/attribution",
        "/voices",
        "/utterances/{ref}/attribute",
        "/voices/{voice_id}/split",
    ):
        assert f"{GATEWAY_PREFIX}/sessions/{{session_id}}{suffix}" in paths, suffix

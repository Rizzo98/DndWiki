"""The public route surface, asserted rather than assumed.

The DM's browser reaches these routes through the gateway, which publishes this
service under a PathPrefix. A route that does not carry that prefix is not a
404 the caller can diagnose: it is answered by whichever service DOES own the
path, with a bare "Not Found" that names nothing.
"""

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


def test_a_plan_that_ran_out_of_budget_is_a_lower_bound_not_a_promise():
    """"About 8 questions to finish" was followed by eight answers, 82% of what
    matters still unattributed, and a panel announcing the session was done.

    The greedy simulation stops at the question cap, so a plan that ends there
    says "at least this many" - and the UI has to be able to tell the two apart.
    """
    capped = review.plan_progress(
        SimpleNamespace(questions_planned=8, questions_asked=3), max_questions=8
    )
    assert capped == {
        "questions": 8,
        "answered": 3,
        "max_questions": 8,
        "is_lower_bound": True,
    }
    converged = review.plan_progress(
        SimpleNamespace(questions_planned=3, questions_asked=1), max_questions=8
    )
    assert converged["is_lower_bound"] is False
    assert converged["questions"] == 3


def test_no_run_is_a_plan_of_nothing_rather_than_a_crash():
    progress = review.plan_progress(None, max_questions=8)
    assert progress == {
        "questions": None,
        "answered": 0,
        "max_questions": 8,
        "is_lower_bound": False,
    }


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

"""State machine unit tests."""

from app.status import ALLOWED_TRANSITIONS, SessionStatus, can_transition


def test_valid_forward_transitions():
    assert can_transition(SessionStatus.UPLOADED, SessionStatus.RECORDED)
    assert can_transition(SessionStatus.RECORDED, SessionStatus.TRANSCRIBING)
    assert can_transition(SessionStatus.TRANSCRIBED, SessionStatus.IDENTIFYING_SPEAKERS)
    assert can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.SUMMARIZING)
    assert can_transition(SessionStatus.SUMMARIZING, SessionStatus.SUMMARY_READY)
    assert can_transition(SessionStatus.SUMMARY_READY, SessionStatus.GENERATING_WIKI)
    assert can_transition(SessionStatus.GENERATING_WIKI, SessionStatus.WIKI_PLAN_READY)
    assert can_transition(SessionStatus.WIKI_PLAN_READY, SessionStatus.APPLYING_WIKI)
    assert can_transition(SessionStatus.APPLYING_WIKI, SessionStatus.CONTENT_READY)
    assert can_transition(SessionStatus.CONTENT_READY, SessionStatus.REVIEWED)
    assert can_transition(SessionStatus.REVIEWED, SessionStatus.PUBLISHED)


def test_summary_review_layer_is_mandatory():
    """The wiki/events phase is unreachable before the DM confirms the summary."""
    assert not can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.GENERATING_WIKI)
    assert not can_transition(SessionStatus.SUMMARIZING, SessionStatus.GENERATING_WIKI)
    assert not can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.CONTENT_READY)
    # the DM may send the draft back for a rewrite as often as they like
    assert can_transition(SessionStatus.SUMMARY_READY, SessionStatus.SUMMARIZING)


def test_change_set_review_layer_is_mandatory():
    """Nothing reaches the wiki before the DM confirms the proposed change
    set: the plan is computed, reviewed, and only then applied."""
    assert not can_transition(SessionStatus.GENERATING_WIKI, SessionStatus.CONTENT_READY)
    assert not can_transition(SessionStatus.SUMMARY_READY, SessionStatus.APPLYING_WIKI)
    assert not can_transition(SessionStatus.GENERATING_WIKI, SessionStatus.APPLYING_WIKI)
    # the DM reviews for as long as they want; applying is the only way out
    assert can_transition(SessionStatus.WIKI_PLAN_READY, SessionStatus.APPLYING_WIKI)


def test_speaker_pending_roundtrip():
    assert can_transition(SessionStatus.IDENTIFYING_SPEAKERS, SessionStatus.SPEAKER_PENDING)
    assert can_transition(SessionStatus.SPEAKER_PENDING, SessionStatus.SPEAKERS_IDENTIFIED)
    assert can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.SPEAKER_PENDING)


def test_failure_allowed_from_active_states():
    for current in ALLOWED_TRANSITIONS:
        if current is not SessionStatus.FAILED:
            assert can_transition(current, SessionStatus.FAILED), current


def test_invalid_transitions():
    # skip states
    assert not can_transition(SessionStatus.UPLOADED, SessionStatus.TRANSCRIBING)
    assert not can_transition(SessionStatus.UPLOADED, SessionStatus.PUBLISHED)
    # backwards
    assert not can_transition(SessionStatus.RECORDED, SessionStatus.UPLOADED)
    assert not can_transition(SessionStatus.PUBLISHED, SessionStatus.SUMMARIZING)
    # failed is terminal EXCEPT the redelivered-message retry (either phase)
    assert not can_transition(SessionStatus.FAILED, SessionStatus.CONTENT_READY)
    assert not can_transition(SessionStatus.FAILED, SessionStatus.UPLOADED)
    assert can_transition(SessionStatus.FAILED, SessionStatus.SUMMARIZING)
    assert can_transition(SessionStatus.FAILED, SessionStatus.GENERATING_WIKI)
    assert can_transition(SessionStatus.FAILED, SessionStatus.APPLYING_WIKI)


def test_unpublish_allowed():
    assert can_transition(SessionStatus.PUBLISHED, SessionStatus.REVIEWED)


def test_pipeline_never_goes_backwards():
    """A session that already generated its wiki never re-enters the summary
    phase: re-running generation would duplicate what the campaign already
    documents. The only way back is unpublishing (published -> reviewed)."""
    for status in (
        SessionStatus.CONTENT_READY,
        SessionStatus.REVIEWED,
        SessionStatus.PUBLISHED,
    ):
        assert not can_transition(status, SessionStatus.SUMMARIZING), status
        assert not can_transition(status, SessionStatus.GENERATING_WIKI), status
    assert not can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.CONTENT_READY)


def test_all_statuses_have_entries():
    assert set(ALLOWED_TRANSITIONS) == set(SessionStatus)

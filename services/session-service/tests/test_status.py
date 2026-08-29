"""State machine unit tests."""

from app.status import ALLOWED_TRANSITIONS, SessionStatus, can_transition


def test_valid_forward_transitions():
    assert can_transition(SessionStatus.UPLOADED, SessionStatus.RECORDED)
    assert can_transition(SessionStatus.RECORDED, SessionStatus.TRANSCRIBING)
    assert can_transition(SessionStatus.TRANSCRIBED, SessionStatus.IDENTIFYING_SPEAKERS)
    assert can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.GENERATING_WIKI)
    assert can_transition(SessionStatus.GENERATING_WIKI, SessionStatus.CONTENT_READY)
    assert can_transition(SessionStatus.CONTENT_READY, SessionStatus.REVIEWED)
    assert can_transition(SessionStatus.REVIEWED, SessionStatus.PUBLISHED)


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
    assert not can_transition(SessionStatus.PUBLISHED, SessionStatus.GENERATING_WIKI)
    # failed is terminal EXCEPT the debug regenerate retry (generating_wiki)
    assert not can_transition(SessionStatus.FAILED, SessionStatus.CONTENT_READY)
    assert not can_transition(SessionStatus.FAILED, SessionStatus.UPLOADED)
    assert can_transition(SessionStatus.FAILED, SessionStatus.GENERATING_WIKI)


def test_unpublish_allowed():
    assert can_transition(SessionStatus.PUBLISHED, SessionStatus.REVIEWED)


def test_debug_regenerate_transitions():
    # the regenerate flow re-enters generating_wiki from content_ready/reviewed
    assert can_transition(SessionStatus.CONTENT_READY, SessionStatus.GENERATING_WIKI)
    assert can_transition(SessionStatus.REVIEWED, SessionStatus.GENERATING_WIKI)
    # failed sessions may retry wiki generation from the same transcript
    assert can_transition(SessionStatus.FAILED, SessionStatus.GENERATING_WIKI)
    # published sessions must be unpublished first; earlier stages stay linear
    assert not can_transition(SessionStatus.PUBLISHED, SessionStatus.GENERATING_WIKI)
    assert not can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.CONTENT_READY)


def test_all_statuses_have_entries():
    assert set(ALLOWED_TRANSITIONS) == set(SessionStatus)

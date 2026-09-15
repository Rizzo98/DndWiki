"""State machine unit tests."""

from app.status import (
    ALLOWED_TRANSITIONS,
    IN_PROGRESS_STATUSES,
    LEGACY_STATUS_ALIASES,
    RESTING_STATUSES,
    SessionStatus,
    can_transition,
    resolve_alias,
)


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


# --- the attribution stage (replaces speaker_pending) -----------------------


def test_attribution_stage_is_walkable_end_to_end():
    assert can_transition(SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.ATTRIBUTING)
    assert can_transition(SessionStatus.ATTRIBUTING, SessionStatus.ATTRIBUTION_READY)
    assert can_transition(SessionStatus.ATTRIBUTION_READY, SessionStatus.ATTRIBUTION_REVIEW)
    assert can_transition(SessionStatus.ATTRIBUTION_REVIEW, SessionStatus.SUMMARIZING)
    assert can_transition(SessionStatus.SUMMARIZING, SessionStatus.SUMMARY_READY)


def test_attribution_review_is_skippable_not_mandatory():
    """The whole point of the redesign: the DM must never be stuck on a speaker
    stage. A session with nothing to ask goes straight on, and one waiting on
    the DM can be finished at any moment."""
    assert can_transition(SessionStatus.ATTRIBUTION_READY, SessionStatus.SUMMARIZING)
    # an explicit recompute is a forward move, not a rewind
    assert can_transition(SessionStatus.ATTRIBUTION_READY, SessionStatus.ATTRIBUTING)
    assert can_transition(SessionStatus.ATTRIBUTION_REVIEW, SessionStatus.SUMMARIZING)
    # ... and the pipeline never requires the review to be COMPLETED first
    assert not can_transition(SessionStatus.ATTRIBUTION_READY, SessionStatus.SUMMARY_READY)


def test_a_new_answer_sends_the_session_back_to_attributing():
    assert can_transition(SessionStatus.ATTRIBUTION_REVIEW, SessionStatus.ATTRIBUTING)


def test_a_redelivered_job_may_re_enter_the_status_it_died_in():
    """A worker killed mid-run leaves the session ON its in-progress status,
    because the failure path is code that never ran.

    When the broker redelivers the job, the worker asks to enter the status the
    session is already in. Refusing that turned a single crash into a session
    stuck forever on 'attributing', with the redelivery acknowledged and
    dropped - which is exactly what happened to the first real session.
    """
    for status in IN_PROGRESS_STATUSES:
        assert can_transition(status, status), status


def test_re_entry_does_not_open_the_door_to_going_backwards():
    """Only the status the session is already in is re-enterable: no in-progress
    status may jump to another one, and no FINISHED status may be re-entered."""
    assert not can_transition(SessionStatus.ATTRIBUTING, SessionStatus.TRANSCRIBING)
    assert not can_transition(SessionStatus.SUMMARIZING, SessionStatus.ATTRIBUTING)
    for done in (
        SessionStatus.SPEAKERS_IDENTIFIED,
        SessionStatus.ATTRIBUTION_READY,
        SessionStatus.ATTRIBUTION_REVIEW,
        SessionStatus.SUMMARY_READY,
        SessionStatus.CONTENT_READY,
    ):
        assert not can_transition(done, done), done


def test_failed_can_retry_the_attribution_phase():
    assert can_transition(SessionStatus.FAILED, SessionStatus.ATTRIBUTING)


def test_refresh_the_summary_is_the_one_controlled_backward_edge():
    """Allowed from the reviewable states only: once the change set is applied,
    re-generating the session's content would duplicate what the campaign
    already documents."""
    assert can_transition(SessionStatus.SUMMARY_READY, SessionStatus.ATTRIBUTING)
    assert can_transition(SessionStatus.WIKI_PLAN_READY, SessionStatus.ATTRIBUTING)
    assert not can_transition(SessionStatus.APPLYING_WIKI, SessionStatus.ATTRIBUTING)
    assert not can_transition(SessionStatus.CONTENT_READY, SessionStatus.ATTRIBUTING)
    assert not can_transition(SessionStatus.PUBLISHED, SessionStatus.ATTRIBUTING)


def test_speaker_pending_survives_one_release_as_an_alias():
    assert resolve_alias(SessionStatus.SPEAKER_PENDING) is SessionStatus.ATTRIBUTION_REVIEW
    assert LEGACY_STATUS_ALIASES[SessionStatus.SPEAKER_PENDING] is (
        SessionStatus.ATTRIBUTION_REVIEW
    )
    # a current status maps to itself
    assert resolve_alias(SessionStatus.ATTRIBUTING) is SessionStatus.ATTRIBUTING
    # the retired status is still a valid VALUE (in-flight sessions hold it)
    assert SessionStatus.SPEAKER_PENDING.value == "speaker_pending"
    assert SessionStatus.SPEAKER_PENDING in ALLOWED_TRANSITIONS


def test_resting_statuses_do_not_block_the_pipeline():
    assert RESTING_STATUSES == {
        SessionStatus.ATTRIBUTION_REVIEW,
        SessionStatus.SUMMARY_READY,
        SessionStatus.WIKI_PLAN_READY,
    }
    for status in RESTING_STATUSES:
        assert can_transition(status, SessionStatus.FAILED)


def test_attribution_statuses_are_deletable_until_the_wiki_is_written():
    """A session may be thrown away until its pages exist; attribution runs well
    before that, so it must not block deletion."""
    from app.status import can_delete

    for status in (
        SessionStatus.ATTRIBUTING,
        SessionStatus.ATTRIBUTION_READY,
        SessionStatus.ATTRIBUTION_REVIEW,
    ):
        assert can_delete(status) is True

"""Session pipeline state machine.

Stored on sessions.status. Workers drive the transitions; the internal
endpoint validates them against ALLOWED_TRANSITIONS so a worker cannot
accidentally jump the pipeline forward or backward.

    uploaded -> recorded -> transcribing -> transcribed -> refining -> refined
        -> identifying_speakers -> speakers_identified
        -> (speaker_pending: DM assigns) -> generating_wiki -> content_ready
        -> reviewed -> published

'refining'/'refined' is the LLM contextual-diarization stage (refiner-service,
between transcription and speaker identification). When the refiner is
disabled, transcription.completed goes straight from 'transcribed' to
'identifying_speakers' as before.

Failures land on 'failed'; 'published' may be pulled back to 'reviewed'
when the DM unpublishes. 'failed' -> 'generating_wiki' is the debug
regenerate retry (same transcript, content-service); all other forward
progress from 'failed' stays blocked.

'generating_wiki' is reachable again from 'content_ready'/'reviewed': the
debug regenerate flow re-runs wiki generation from the same transcript
(content-service POST /api/content/sessions/{id}/regenerate).
"""

from enum import Enum


class SessionStatus(str, Enum):
    """All valid values of sessions.status."""

    UPLOADED = "uploaded"
    RECORDED = "recorded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    REFINING = "refining"
    REFINED = "refined"
    IDENTIFYING_SPEAKERS = "identifying_speakers"
    SPEAKERS_IDENTIFIED = "speakers_identified"
    SPEAKER_PENDING = "speaker_pending"
    GENERATING_WIKI = "generating_wiki"
    CONTENT_READY = "content_ready"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.UPLOADED: {SessionStatus.RECORDED, SessionStatus.FAILED},
    SessionStatus.RECORDED: {SessionStatus.TRANSCRIBING, SessionStatus.FAILED},
    SessionStatus.TRANSCRIBING: {SessionStatus.TRANSCRIBED, SessionStatus.FAILED},
    SessionStatus.TRANSCRIBED: {
        SessionStatus.REFINING,
        SessionStatus.IDENTIFYING_SPEAKERS,  # refiner disabled -> direct path
        SessionStatus.FAILED,
    },
    SessionStatus.REFINING: {SessionStatus.REFINED, SessionStatus.FAILED},
    SessionStatus.REFINED: {SessionStatus.IDENTIFYING_SPEAKERS, SessionStatus.FAILED},
    SessionStatus.IDENTIFYING_SPEAKERS: {
        SessionStatus.SPEAKERS_IDENTIFIED,
        SessionStatus.SPEAKER_PENDING,
        SessionStatus.FAILED,
    },
    SessionStatus.SPEAKER_PENDING: {SessionStatus.SPEAKERS_IDENTIFIED, SessionStatus.FAILED},
    SessionStatus.SPEAKERS_IDENTIFIED: {
        SessionStatus.GENERATING_WIKI,
        SessionStatus.SPEAKER_PENDING,
        SessionStatus.FAILED,
    },
    SessionStatus.GENERATING_WIKI: {SessionStatus.CONTENT_READY, SessionStatus.FAILED},
    # content_ready/reviewed/failed -> generating_wiki: debug regenerate (same transcript)
    SessionStatus.CONTENT_READY: {SessionStatus.REVIEWED, SessionStatus.GENERATING_WIKI, SessionStatus.FAILED},
    SessionStatus.REVIEWED: {SessionStatus.PUBLISHED, SessionStatus.CONTENT_READY, SessionStatus.GENERATING_WIKI, SessionStatus.FAILED},
    SessionStatus.PUBLISHED: {SessionStatus.REVIEWED, SessionStatus.FAILED},
    # failed is terminal for forward progress; the debug regenerate flow
    # may retry wiki generation from the same transcript (content-service
    # POST /api/content/sessions/{id}/regenerate)
    SessionStatus.FAILED: {SessionStatus.GENERATING_WIKI},
}


def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
    """Whether sessions.status may move from current to target."""
    return target in ALLOWED_TRANSITIONS.get(current, set())
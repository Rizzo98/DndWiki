"""Session pipeline state machine.

Stored on sessions.status. Workers drive the transitions; the internal
endpoint validates them against ALLOWED_TRANSITIONS so a worker cannot
accidentally jump the pipeline forward or backward.

    uploaded -> recorded -> transcribing -> transcribed -> refining -> refined
        -> identifying_speakers -> speakers_identified
        -> attributing -> attribution_ready
        -> attribution_review   (RESTING, skippable, non-blocking)
        -> summarizing -> summary_ready
        -> generating_wiki -> wiki_plan_ready -> applying_wiki -> content_ready
        -> reviewed -> published

'attributing'/'attribution_ready'/'attribution_review' is the ATTRIBUTION stage
(attribution-service). The engine computes a per-utterance belief about who said
what; 'attribution_review' is where the DM answers its questions. It replaces
'speaker_pending' - which BLOCKED the pipeline until every diarized label was
confirmed by hand - with a resting state the DM can skip, because the engine
reports how much of the session it is actually confident about and the rest is
recorded as unresolved rather than invented.

'refining'/'refined' is the LLM contextual-diarization stage (refiner-service,
between transcription and speaker identification). When the refiner is
disabled, transcription.completed goes straight from 'transcribed' to
'identifying_speakers' as before.

'summarizing'/'summary_ready' is the session-summary REVIEW LAYER: the
transcript is distilled into a DRAFT session summary the DM reviews on the
session page. The session parks on 'summary_ready' until the DM either asks
for a rewrite with feedback ('summarizing' again) or confirms the summary
('generating_wiki').

'generating_wiki' -> 'wiki_plan_ready' -> 'applying_wiki' is the CHANGE-SET
REVIEW LAYER: the confirmed summary is turned into a PROPOSED change set (the
pages and timeline entries that WOULD be written, with a per-field diff
against what the wiki already documents), nothing is written yet. The DM
inspects, edits or drops single changes and confirms the set, and only then
does 'applying_wiki' write the pages and timeline entries.

Failures land on 'failed'; 'published' may be pulled back to 'reviewed'
when the DM unpublishes. 'failed' -> 'summarizing'/'generating_wiki' is the
RETRY edge: the worker marks the session failed, the message is redelivered
and the same phase runs again. All other forward progress from 'failed' stays
blocked, so a failed session never silently restarts.

DELETION follows the same line: the DM may delete a session until the wiki
part of it exists ('applying_wiki' and later block it), because the pages and
timeline entries a session wrote must never outlive the session itself.

The pipeline never goes backwards: a session that reached 'content_ready' or
'published' can only move forward (or be unpublished back to 'reviewed').
Re-generating a session's wiki content would duplicate what the campaign
already documents, so it is not reachable through this machine at all.
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
    # Retired as a BLOCKING state by the attribution redesign; still accepted so
    # in-flight sessions survive the deploy (see LEGACY_STATUS_ALIASES below).
    SPEAKER_PENDING = "speaker_pending"
    # The attribution stage: 'attributing' is the engine computing a revision,
    # 'attribution_ready' is the belief being written down, and
    # 'attribution_review' is the RESTING state where the DM answers the
    # engine's questions. It is skippable and never blocks the pipeline.
    ATTRIBUTING = "attributing"
    ATTRIBUTION_READY = "attribution_ready"
    ATTRIBUTION_REVIEW = "attribution_review"
    SUMMARIZING = "summarizing"
    SUMMARY_READY = "summary_ready"
    GENERATING_WIKI = "generating_wiki"
    WIKI_PLAN_READY = "wiki_plan_ready"
    APPLYING_WIKI = "applying_wiki"
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
        SessionStatus.SPEAKER_PENDING,  # legacy: pre-redesign identification
        SessionStatus.FAILED,
    },
    SessionStatus.SPEAKER_PENDING: {
        SessionStatus.SPEAKERS_IDENTIFIED,
        SessionStatus.ATTRIBUTING,  # a session parked here can still be attributed
        SessionStatus.FAILED,
    },
    SessionStatus.SPEAKERS_IDENTIFIED: {
        SessionStatus.ATTRIBUTING,  # the engine takes over
        SessionStatus.SUMMARIZING,  # attribution disabled / engine unavailable
        SessionStatus.SPEAKER_PENDING,  # legacy blocking panel
        SessionStatus.FAILED,
    },
    # The attribution stage. 'attribution_review' is the resting state: the DM
    # may leave it at any time (Finish / Skip), and nothing downstream waits for
    # it -- unlike the old 'speaker_pending', which blocked the pipeline until
    # every label was confirmed.
    SessionStatus.ATTRIBUTING: {
        SessionStatus.ATTRIBUTION_READY,
        SessionStatus.ATTRIBUTION_REVIEW,
        SessionStatus.FAILED,
    },
    SessionStatus.ATTRIBUTION_READY: {
        SessionStatus.ATTRIBUTION_REVIEW,
        SessionStatus.SUMMARIZING,  # nothing worth asking; go straight on
        # An explicit recompute: 'attribution.recompute' exists so a session can
        # be re-attributed after the DM enrolled a voiceprint or answered the
        # questions that made it worth asking. Without this edge that event did
        # nothing at all on a session that had finished attributing once.
        SessionStatus.ATTRIBUTING,
        SessionStatus.FAILED,
    },
    SessionStatus.ATTRIBUTION_REVIEW: {
        SessionStatus.ATTRIBUTING,  # a new answer triggers a recompute
        SessionStatus.SUMMARIZING,  # DM pressed Finish / the engine stopped
        SessionStatus.FAILED,
    },
    # The session summary is the reviewable intermediate layer: 'summarizing'
    # is the first draft (or a rewrite the DM asked for) and 'summary_ready'
    # waits for the DM to confirm it before any page/event is created.
    SessionStatus.SUMMARIZING: {SessionStatus.SUMMARY_READY, SessionStatus.FAILED},
    SessionStatus.SUMMARY_READY: {
        SessionStatus.GENERATING_WIKI,  # DM confirmed the summary
        SessionStatus.SUMMARIZING,  # DM asked for a rewrite (with feedback)
        # "Refresh the summary with the improved attribution": the one
        # controlled BACKWARD edge, and the reason it is safe is that the change
        # set has not been applied yet -- re-generating after applying_wiki
        # would duplicate what the campaign already documents.
        SessionStatus.ATTRIBUTING,
        SessionStatus.FAILED,
    },
    # The confirmed summary is turned into a PROPOSED CHANGE SET first:
    # 'generating_wiki' computes it, 'wiki_plan_ready' waits for the DM to
    # review/edit/confirm it, and only 'applying_wiki' writes to the wiki.
    SessionStatus.GENERATING_WIKI: {SessionStatus.WIKI_PLAN_READY, SessionStatus.FAILED},
    SessionStatus.WIKI_PLAN_READY: {
        SessionStatus.APPLYING_WIKI,  # DM confirmed the proposed changes
        # Same backward edge as summary_ready: still reviewable, still safe,
        # because the change set has not been written.
        SessionStatus.ATTRIBUTING,
        SessionStatus.FAILED,
    },
    SessionStatus.APPLYING_WIKI: {SessionStatus.CONTENT_READY, SessionStatus.FAILED},
    SessionStatus.CONTENT_READY: {SessionStatus.REVIEWED, SessionStatus.FAILED},
    SessionStatus.REVIEWED: {
        SessionStatus.PUBLISHED,
        SessionStatus.CONTENT_READY,
        SessionStatus.FAILED,
    },
    SessionStatus.PUBLISHED: {SessionStatus.REVIEWED, SessionStatus.FAILED},
    # failed is terminal for forward progress; a redelivered message may retry
    # the phase it failed in (summary draft/rewrite -> summarizing, change-set
    # computation -> generating_wiki, change-set application -> applying_wiki)
    SessionStatus.FAILED: {
        SessionStatus.ATTRIBUTING,
        SessionStatus.SUMMARIZING,
        SessionStatus.GENERATING_WIKI,
        SessionStatus.APPLYING_WIKI,
    },
}


#: Statuses retired by the attribution redesign, mapped to their replacement.
#: Accepted for one release so a session already parked on the old status
#: survives a deploy; removed once no session can be in them.
LEGACY_STATUS_ALIASES: dict[SessionStatus, SessionStatus] = {
    SessionStatus.SPEAKER_PENDING: SessionStatus.ATTRIBUTION_REVIEW,
}

#: Statuses that are RESTING: the pipeline is waiting for the DM, not working.
#: None of them block anything -- the DM can finish, skip or come back later.
RESTING_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.ATTRIBUTION_REVIEW,
        SessionStatus.SUMMARY_READY,
        SessionStatus.WIKI_PLAN_READY,
    }
)


def resolve_alias(status: SessionStatus) -> SessionStatus:
    """Map a retired status onto its replacement (identity for current ones)."""
    return LEGACY_STATUS_ALIASES.get(status, status)


#: Statuses a session can no longer be DELETED from. From 'applying_wiki' on,
#: the pipeline is writing (or already wrote) the session's pages and timeline
#: entries: the session record cannot disappear from under wiki content that
#: points at it, so deletion stops here. Everything before it (including a
#: failed session) can still be thrown away.
DELETE_BLOCKED_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.APPLYING_WIKI,
        SessionStatus.CONTENT_READY,
        SessionStatus.REVIEWED,
        SessionStatus.PUBLISHED,
    }
)


def can_delete(status: SessionStatus) -> bool:
    """Whether a session in this status may still be deleted by the DM."""
    return status not in DELETE_BLOCKED_STATUSES


#: Statuses that mean "a worker is doing this right now". Each one is owned by
#: exactly one job, and the ONLY way out of it is that job finishing (or failing
#: and marking the session failed).
#:
#: That makes re-entry load-bearing rather than a nicety. A worker killed
#: mid-run - a restart, a deploy, an OOM - leaves the session sitting on its
#: in-progress status, having never reached the failure path. When RabbitMQ
#: redelivers the job, the worker asks to enter the status it is ALREADY in;
#: refusing that turns one crash into a session that is stuck forever, with the
#: redelivery quietly acknowledged and dropped.
IN_PROGRESS_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.TRANSCRIBING,
        SessionStatus.REFINING,
        SessionStatus.IDENTIFYING_SPEAKERS,
        SessionStatus.ATTRIBUTING,
        SessionStatus.SUMMARIZING,
        SessionStatus.GENERATING_WIKI,
        SessionStatus.APPLYING_WIKI,
    }
)


def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
    """Whether sessions.status may move from current to target.

    Re-entering an in-progress status is allowed and is a no-op for the caller:
    it is a redelivered job retrying the phase its previous attempt died in.
    """
    if current == target and current in IN_PROGRESS_STATUSES:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())

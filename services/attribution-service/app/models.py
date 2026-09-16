"""SQLAlchemy models for dnd_attribution (docs/attribution-model.md S3.2).

The schema encodes the whole point of the redesign: an utterance carries a
POSTERIOR over candidates, and a diarization label is stored as evidence
('diar_label') rather than as an identity. Nothing here is a 1:1 mapping, so
'this cluster is three people' and 'this person is three clusters' are both
representable at zero cost.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from dnd_common.db import Base
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

# The status vocabulary is defined in app.statuses (dependency-free) and
# re-exported here so ORM consumers have one import; the ORDER is the
# monotonicity rule of docs/attribution-model.md S7.3.
from app.statuses import (
    ATTRIBUTION_STATUSES,
    CONFIDENT_STATUSES,
    STATUS_RANK,
)

__all__ = [
    "ATTRIBUTION_STATUSES",
    "CONFIDENT_STATUSES",
    "STATUS_RANK",
    "AttributionCalibration",
    "MemberCapability",
    "MemberVoiceModel",
    "PropagationEvent",
    "ReviewQuestion",
    "ReviewRun",
    "SessionBeliefStats",
    "SessionScene",
    "Utterance",
    "UtteranceAttribution",
    "UtteranceEvidence",
    "VoiceIdentity",
]


class Utterance(Base):
    """One thing somebody said, with every attribution input on the row."""

    __tablename__ = "utterances"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String(32), nullable=False)  # 'u_00412'

    start_sec: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    end_sec: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    gap_before_sec: Mapped[float | None] = mapped_column(Numeric(10, 3))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))

    # provenance: which diarization segments this utterance was built from
    segment_indices: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )

    # --- diarization: EVIDENCE, never identity ------------------------------
    diar_label: Mapped[str | None] = mapped_column(String(32))
    diar_chunk: Mapped[int | None] = mapped_column(Integer)
    diar_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    asr_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))

    # --- layer 2 link (nullable: unembedded / noisy / overlapped) ----------
    voice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_identities.id", ondelete="SET NULL")
    )
    voice_quality: Mapped[float | None] = mapped_column(Numeric(5, 4))

    # --- semantics from the identity-evidence pass -------------------------
    kind: Mapped[str | None] = mapped_column(String(16))
    voice_mode: Mapped[str | None] = mapped_column(String(16))
    gist: Mapped[str | None] = mapped_column(Text)
    stakes: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.5)
    capability_reqs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    addressed_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    claimed_names: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    #: What the utterance is ABOUT, for phrasing a question the DM can answer
    #: from memory (S8.2): {'quote': ..., 'audio': {'start':..., 'end':...}}
    hook: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VoiceIdentity(Base):
    """Layer 2: an anonymous, session-scoped grouping of observations."""

    __tablename__ = "voice_identities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    handle: Mapped[str] = mapped_column(String(16), nullable=False)  # 'V1', display only
    start_sec: Mapped[float | None] = mapped_column(Numeric(10, 3))
    end_sec: Mapped[float | None] = mapped_column(Numeric(10, 3))
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speech_sec: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    centroid: Mapped[list[float] | None] = mapped_column(JSONB)
    purity: Mapped[float | None] = mapped_column(Numeric(5, 4))
    impurity_evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="session")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    split_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_identities.id")
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_identities.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UtteranceEvidence(Base):
    """Append-only evidence log. The belief is derivable from this alone."""

    __tablename__ = "utterance_evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    utterance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("utterances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="engine")
    source_ref: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    log_lr: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    weight: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UtteranceAttribution(Base):
    """The current belief about one utterance (a cache of the evidence log)."""

    __tablename__ = "utterance_attributions"

    utterance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("utterances.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    posterior: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    best_candidate: Mapped[str | None] = mapped_column(String(64))
    best_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    best_character: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    margin: Mapped[float | None] = mapped_column(Numeric(5, 4))
    entropy: Mapped[float | None] = mapped_column(Numeric(6, 4))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReviewQuestion(Base):
    __tablename__ = "review_questions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    hook: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    options: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    target_utterances: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    target_voices: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    #: The member a presence question is about. A content question encodes its
    #: subject in the option keys (the roster list); a presence question offers
    #: yes/no, so the member has to be carried separately or the answer arrives
    #: with nothing to apply it to.
    target_member: Mapped[str | None] = mapped_column(String(64))
    expected_gain: Mapped[float | None] = mapped_column(Numeric(8, 4))
    gain_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False, default=1)
    score: Mapped[float | None] = mapped_column(Numeric(8, 4))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReviewRun(Base):
    __tablename__ = "review_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    questions_planned: Mapped[int | None] = mapped_column(Integer)
    questions_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_idk: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_before: Mapped[float | None] = mapped_column(Numeric(5, 4))
    coverage_after: Mapped[float | None] = mapped_column(Numeric(5, 4))
    entropy_before: Mapped[float | None] = mapped_column(Numeric(10, 4))
    entropy_after: Mapped[float | None] = mapped_column(Numeric(10, 4))
    engine_version: Mapped[str | None] = mapped_column(String(32))
    calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    converged: Mapped[bool | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PropagationEvent(Base):
    __tablename__ = "propagation_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    question_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_questions.id", ondelete="SET NULL")
    )
    resolved_utterances: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_sec: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    voice_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False, default=list)
    learned: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemberCapability(Base):
    __tablename__ = "member_capabilities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    polarity: Mapped[str] = mapped_column(String(8), nullable=False, default="can")
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.5)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemberVoiceModel(Base):
    __tablename__ = "member_voice_models"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    qdrant_point_id: Mapped[str] = mapped_column(Text, nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean_quality: Mapped[float | None] = mapped_column(Numeric(5, 4))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    voice_mode: Mapped[str | None] = mapped_column(String(16))
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AttributionCalibration(Base):
    __tablename__ = "attribution_calibrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    n_labeled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_confident_rate: Mapped[float | None] = mapped_column(Numeric(5, 4))
    fitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SessionScene(Base):
    """One stretch of a session in one place, with who is there (S12.6).

    Persisted because it is the only representation of "where this happens and
    who is in the room" the system has, and three different readers need it:

    * the ENGINE, which applies the stated absences as a per-moment exclusion -
      that part is baked into the belief's potentials and would work without this
      table, but then nothing could explain WHY a member was excluded;
    * the DM, who is the only one able to correct a reading that got the party's
      movements wrong, and who cannot correct what nobody shows them;
    * the wiki, for which a session's locations are a fact worth having.

    'present_member_ids'/'absent_member_ids' are resolved at write time against
    the roster: the names are what the reading said (kept verbatim, so a name that
    resolves to nobody is visible rather than silently dropped), and the ids are
    what the engine keys candidates by.
    """

    __tablename__ = "session_scenes"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    index: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    start_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sec: Mapped[float | None] = mapped_column(Numeric(12, 3))
    end_sec: Mapped[float | None] = mapped_column(Numeric(12, 3))
    first_ref: Mapped[str | None] = mapped_column(String(32))
    last_ref: Mapped[str | None] = mapped_column(String(32))

    location: Mapped[str] = mapped_column(String(200), nullable=False, default="unclear")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    present_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    absent_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    npcs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    present_member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    absent_member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SessionBeliefStats(Base):
    """Per-session rollup: the numbers the review card and the metrics read.

    Not in the design's table list, but it is the cheapest way to answer
    'coverage / unresolved / planned questions' without re-running inference on
    every page load, and coverage is what the DM actually sees.
    """

    __tablename__ = "session_belief_stats"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    coverage: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    unresolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_stakes: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False, default=0)
    total_speech_sec: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    entropy: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    questions_planned: Mapped[int | None] = mapped_column(Integer)
    converged: Mapped[bool | None] = mapped_column()
    #: The belief the review API rebuilds its Propagator from. A CACHE, never a
    #: source of truth: utterance_evidence is the log the belief is derived
    #: from, and this snapshot only saves re-running evidence extraction (LLM
    #: calls) on every page load. A snapshot whose engine_version does not match
    #: the running engine is discarded and the pass is recomputed.
    belief: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    engine_version: Mapped[str | None] = mapped_column(String(32))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

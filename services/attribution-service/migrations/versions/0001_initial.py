"""initial attribution schema (docs/attribution-model.md S3.2)

Revision ID: 0001
Revises:
Create Date: 2025-01-04

The five-layer identity model, in one database:

  utterances            layer 1 - the unit of attribution (one posterior each)
  voice_identities      layer 2 - anonymous, session-scoped, WITH a purity estimate
  utterance_evidence    the append-only log the belief is derived from
  utterance_attributions  the current belief (a cache, never a source of truth)
  review_questions/runs the review
  member_capabilities/member_voice_models  campaign-level learning
  attribution_calibrations  per-campaign score -> log LR maps

The one thing a reader should notice: there is no column anywhere that maps a
diarization label to a person. 'utterances.diar_label' is evidence; 'utterances.
voice_id' points at an anonymous grouping; the posterior column is a
distribution.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("handle", sa.String(length=16), nullable=False),
        sa.Column("start_sec", sa.Numeric(10, 3), nullable=True),
        sa.Column("end_sec", sa.Numeric(10, 3), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speech_sec", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("centroid", JSONB(), nullable=True),
        sa.Column("purity", sa.Numeric(5, 4), nullable=True),
        sa.Column("impurity_evidence", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="session"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("split_from_id", sa.Uuid(), sa.ForeignKey("voice_identities.id"), nullable=True),
        sa.Column("merged_into_id", sa.Uuid(), sa.ForeignKey("voice_identities.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "handle", name="uq_voice_identities_session_handle"),
    )
    op.create_index("ix_voice_identities_session_id", "voice_identities", ["session_id"])

    op.create_table(
        "utterances",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(length=32), nullable=False),
        sa.Column("start_sec", sa.Numeric(10, 3), nullable=False),
        sa.Column("end_sec", sa.Numeric(10, 3), nullable=False),
        sa.Column("gap_before_sec", sa.Numeric(10, 3), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("segment_indices", ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("diar_label", sa.String(length=32), nullable=True),
        sa.Column("diar_chunk", sa.Integer(), nullable=True),
        sa.Column("diar_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("asr_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("voice_id", sa.Uuid(), sa.ForeignKey("voice_identities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("voice_quality", sa.Numeric(5, 4), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=True),
        sa.Column("voice_mode", sa.String(length=16), nullable=True),
        sa.Column("gist", sa.Text(), nullable=True),
        sa.Column("stakes", sa.Numeric(5, 4), nullable=False, server_default="0.5"),
        sa.Column("capability_reqs", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("addressed_names", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("claimed_names", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("hook", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "ordinal", name="uq_utterances_session_ordinal"),
    )
    op.create_index("ix_utterances_session_start", "utterances", ["session_id", "start_sec"])
    op.create_index("ix_utterances_session_voice", "utterances", ["session_id", "voice_id"])

    op.create_table(
        "utterance_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("utterance_id", sa.Uuid(), sa.ForeignKey("utterances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="engine"),
        sa.Column("source_ref", sa.String(length=64), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("log_lr", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("weight", sa.Numeric(5, 4), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_utterance_evidence_utterance", "utterance_evidence", ["utterance_id"])
    op.create_index("ix_utterance_evidence_session_channel", "utterance_evidence", ["session_id", "channel"])

    op.create_table(
        "utterance_attributions",
        sa.Column("utterance_id", sa.Uuid(), sa.ForeignKey("utterances.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("posterior", JSONB(), nullable=False),
        sa.Column("best_candidate", sa.String(length=64), nullable=True),
        sa.Column("best_member_id", sa.Uuid(), nullable=True),
        sa.Column("best_character", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("margin", sa.Numeric(5, 4), nullable=True),
        sa.Column("entropy", sa.Numeric(6, 4), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_utterance_attributions_session_status", "utterance_attributions", ["session_id", "status"])

    op.create_table(
        "review_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("hook", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("options", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("target_utterances", ARRAY(sa.Uuid()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("target_voices", ARRAY(sa.Uuid()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("expected_gain", sa.Numeric(8, 4), nullable=True),
        sa.Column("gain_detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cost", sa.Numeric(4, 2), nullable=False, server_default="1"),
        sa.Column("score", sa.Numeric(8, 4), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="candidate"),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_review_questions_session_status", "review_questions", ["session_id", "status", "score"])

    op.create_table(
        "review_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("questions_planned", sa.Integer(), nullable=True),
        sa.Column("questions_asked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_idk", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_before", sa.Numeric(5, 4), nullable=True),
        sa.Column("coverage_after", sa.Numeric(5, 4), nullable=True),
        sa.Column("entropy_before", sa.Numeric(10, 4), nullable=True),
        sa.Column("entropy_after", sa.Numeric(10, 4), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=True),
        sa.Column("calibration_id", sa.Uuid(), nullable=True),
        sa.Column("converged", sa.Boolean(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "propagation_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("review_questions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_utterances", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_sec", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("voice_ids", ARRAY(sa.Uuid()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("learned", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_propagation_events_session", "propagation_events", ["session_id"])

    op.create_table(
        "member_capabilities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("polarity", sa.String(length=8), nullable=False, server_default="can"),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0.5"),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "campaign_id", "member_id", "capability", "polarity",
            name="uq_member_capabilities_key",
        ),
    )

    op.create_table(
        "member_voice_models",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("qdrant_point_id", sa.Text(), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean_quality", sa.Numeric(5, 4), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("voice_mode", sa.String(length=16), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_member_voice_models_campaign_member", "member_voice_models", ["campaign_id", "member_id"])

    op.create_table(
        "attribution_calibrations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("n_labeled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("false_confident_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("fitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("campaign_id", "model_version", name="uq_attribution_calibrations_key"),
    )

    op.create_table(
        "session_belief_stats",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("coverage", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("unresolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_stakes", sa.Numeric(8, 4), nullable=False, server_default="0"),
        sa.Column("total_speech_sec", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("entropy", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("questions_planned", sa.Integer(), nullable=True),
        sa.Column("converged", sa.Boolean(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_session_belief_stats_campaign", "session_belief_stats", ["campaign_id"])


def downgrade() -> None:
    for table in (
        "session_belief_stats",
        "attribution_calibrations",
        "member_voice_models",
        "member_capabilities",
        "propagation_events",
        "review_runs",
        "review_questions",
        "utterance_attributions",
        "utterance_evidence",
        "utterances",
        "voice_identities",
    ):
        op.drop_table(table)

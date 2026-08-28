"""initial schema: wiki_pages, page_versions, page_relations, timeline_events

Revision ID: 0001
Revises:
Create Date: 2025-01-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "content_json",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("source_session_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("campaign_id", "slug", name="uq_wiki_pages_campaign_slug"),
    )
    op.create_index("ix_wiki_pages_campaign_id", "wiki_pages", ["campaign_id"])
    op.create_index("ix_wiki_pages_kind", "wiki_pages", ["kind"])
    op.create_index("ix_wiki_pages_slug", "wiki_pages", ["slug"])
    op.create_index("ix_wiki_pages_status", "wiki_pages", ["status"])

    op.create_table(
        "page_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "content_json",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("page_id", "version_no", name="uq_page_versions_page_no"),
    )
    op.create_index("ix_page_versions_page_id", "page_versions", ["page_id"])

    op.create_table(
        "page_relations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_page_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "page_id", "related_page_id", "relation_type", name="uq_page_relations_triple"
        ),
    )
    op.create_index("ix_page_relations_page_id", "page_relations", ["page_id"])
    op.create_index("ix_page_relations_related_page_id", "page_relations", ["related_page_id"])

    op.create_table(
        "timeline_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_pages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("in_world_date", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source_session_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_timeline_events_campaign_id", "timeline_events", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_timeline_events_campaign_id", table_name="timeline_events")
    op.drop_table("timeline_events")
    op.drop_index("ix_page_relations_related_page_id", table_name="page_relations")
    op.drop_index("ix_page_relations_page_id", table_name="page_relations")
    op.drop_table("page_relations")
    op.drop_index("ix_page_versions_page_id", table_name="page_versions")
    op.drop_table("page_versions")
    op.drop_index("ix_wiki_pages_status", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_slug", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_kind", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_campaign_id", table_name="wiki_pages")
    op.drop_table("wiki_pages")

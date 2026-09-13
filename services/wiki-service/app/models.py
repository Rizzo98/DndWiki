"""SQLAlchemy models for the dnd_wiki database (database-per-service).

Mirrors docs/data-model.md:

- wiki_pages      - the page itself; status/visibility drive who may see it
- page_versions   - immutable snapshots written on every content change
- page_relations  - proposed cross-references between pages
- timeline_events - in-world calendar entries for a campaign

Permission invariants (enforced in the service layer, not the DB):

- players read only status='published' AND visibility='public' pages
- the DM reads everything in their campaign
- a page is never hard-deleted; 'archived' + 'hidden' cover removal
"""

import uuid
from datetime import datetime

from dnd_common.db import Base
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class WikiPage(Base):
    """A wiki page in a campaign (character, location, event, ...)."""

    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("campaign_id", "slug", name="uq_wiki_pages_campaign_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # character | location | faction | item | quest (see app/schemas.py)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # body blocks + front matter; JSONB on Postgres, JSON in tests (SQLite)
    content_json: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    # draft | published | archived (never a 'pending review' state)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft", index=True
    )
    # public | dm_only | hidden
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public", server_default="public"
    )
    # 0..1 confidence from the LLM draft (content-service)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PageVersion(Base):
    """Immutable snapshot of a page's content at a point in time.

    version_no is a per-page monotonic counter (1, 2, ...) written by the
    service layer; it makes "newest version first" deterministic even when two
    snapshots land within the same timestamp tick.
    """

    __tablename__ = "page_versions"
    __table_args__ = (UniqueConstraint("page_id", "version_no", name="uq_page_versions_page_no"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_json: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    change_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PageRelation(Base):
    """Proposed cross-reference between two pages of the same campaign."""

    __tablename__ = "page_relations"
    __table_args__ = (
        UniqueConstraint(
            "page_id", "related_page_id", "relation_type", name="uq_page_relations_triple"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # appears_in | member_of | allied_with | led_by | ...
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TimelineEvent(Base):
    """A campaign timeline entry with an in-world date."""

    __tablename__ = "timeline_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="SET NULL")
    )
    # campaign-specific calendar string, e.g. "17 Ches 1492 DR"
    in_world_date: Mapped[str | None] = mapped_column(String(256))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

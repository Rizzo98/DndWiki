"""campaign_notes persistence: the DM's own planning notes.

Straight CRUD, with one rule that belongs to the data rather than to the API:
every lookup is scoped by campaign_id as well as by note id. A note id is a
UUID and guessing one is impractical, but the campaign scope is what makes the
authorization above it sufficient - the caller is checked against the campaign
the route names, so the route must never read a note by id alone and hand back
a row from somebody else's campaign.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NOTE_DRAFT, NOTE_READY, CampaignNote

#: How much of a note's body the list view carries (a preview, not the note).
EXCERPT_CHARS = 280

STATUSES = (NOTE_DRAFT, NOTE_READY)


async def list_notes(db: AsyncSession, campaign_id: UUID) -> list[CampaignNote]:
    """Every note of a campaign, newest change first.

    Updated-descending rather than created-descending: the toolkit is a working
    surface, so the note the DM just wrote is the one they want at the top.
    """
    result = await db.scalars(
        select(CampaignNote)
        .where(CampaignNote.campaign_id == campaign_id)
        .order_by(CampaignNote.updated_at.desc(), CampaignNote.id.desc())
    )
    return list(result)


async def get_note(db: AsyncSession, campaign_id: UUID, note_id: UUID) -> CampaignNote | None:
    """One note of one campaign (never a note of another campaign)."""
    return await db.scalar(
        select(CampaignNote).where(
            CampaignNote.id == note_id, CampaignNote.campaign_id == campaign_id
        )
    )


async def get_notes_by_ids(
    db: AsyncSession, campaign_id: UUID, note_ids: list[UUID]
) -> list[CampaignNote]:
    """The notes of a selection, in the order they were asked for.

    Ids that do not resolve (deleted, or belonging to another campaign) are
    silently absent: the caller compares what it got with what it asked for and
    refuses a generation over notes it could not read.
    """
    if not note_ids:
        return []
    rows = await db.scalars(
        select(CampaignNote).where(
            CampaignNote.campaign_id == campaign_id, CampaignNote.id.in_(note_ids)
        )
    )
    by_id = {note.id: note for note in rows}
    return [by_id[note_id] for note_id in note_ids if note_id in by_id]


async def create_note(
    db: AsyncSession,
    campaign_id: UUID,
    *,
    title: str,
    body: str = "",
    status: str = NOTE_DRAFT,
    user_id: UUID | None = None,
) -> CampaignNote:
    note = CampaignNote(
        campaign_id=campaign_id,
        title=title.strip() or "Untitled note",
        body=body,
        status=status if status in STATUSES else NOTE_DRAFT,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def update_note(
    db: AsyncSession,
    note: CampaignNote,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    user_id: UUID | None = None,
) -> CampaignNote:
    """Apply the fields the caller sent; None means "leave it as it is".

    An empty title is refused by the API rather than silently rewritten here:
    a blank title would rename the note to "Untitled note" behind the DM's back
    while they are clearing the field to retype it.
    """
    if title is not None:
        note.title = title.strip()[:255] or note.title
    if body is not None:
        note.body = body
    if status is not None and status in STATUSES:
        note.status = status
    note.updated_by = user_id
    await db.commit()
    await db.refresh(note)
    return note


async def delete_note(db: AsyncSession, campaign_id: UUID, note_id: UUID) -> bool:
    """Hard-delete a note; True when a row was actually removed.

    Notes are the DM's scratchpad, not the campaign record: nothing links to
    them (a proposal keeps the note IDS it read, and a missing note there is
    just a note that no longer exists), so there is no archive state to keep.
    """
    result = await db.execute(
        delete(CampaignNote).where(
            CampaignNote.id == note_id, CampaignNote.campaign_id == campaign_id
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def count_notes(db: AsyncSession, campaign_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(CampaignNote).where(
                CampaignNote.campaign_id == campaign_id
            )
        )
        or 0
    )


def excerpt(note: CampaignNote) -> str:
    """A one-line preview of a note for the list."""
    text = " ".join((note.body or "").split())
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS].rstrip() + "…"

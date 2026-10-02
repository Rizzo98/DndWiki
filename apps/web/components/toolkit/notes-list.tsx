// NotesList — the DM's planning notes, as a list you pick from.
//
// Writing and generating are two different acts, and the list keeps them apart:
// the checkbox on each row is "build on this one", the row itself is "open this
// one". Selecting is how a note enters a generation, so the list is the place
// the DM chooses what the wiki will be made of - and the place that shows what
// the last generation actually read (its notes stay ticked).
//
// A note is never deleted from here; that lives in the editor, where the DM can
// see what they are about to lose.
//
// The excerpt is rendered, not printed: a note's "@bree" is a page, so it shows
// in the link colour and opens that page. The title is what opens the note.

"use client";

import { Alert, EmptyState } from "@/components/ui";
import { LinkedText, type LinkIndex } from "@/components/linked-text";
import { RlIcon, RlPanel, RlPanelHead, RlTag } from "@/components/ravenlore";
import { NOTE_STATUS_LABELS, type NoteSummary } from "@/lib/api";

function stamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString();
}

export function NotesList({
  notes,
  activeId,
  selected,
  loading,
  error,
  busy,
  campaignId,
  linkIndex,
  onOpen,
  onToggleSelected,
  onSelectAll,
  onClearSelection,
  onCreate,
}: {
  notes: NoteSummary[];
  activeId: string | null;
  /** The campaign the notes' page tags point into. */
  campaignId?: string;
  /** Page lookup for those tags (and for the page names a note mentions). */
  linkIndex?: LinkIndex | null;
  /** note id -> picked for the next generation. */
  selected: Record<string, boolean>;
  loading: boolean;
  error?: string | null;
  busy: boolean;
  onOpen: (noteId: string) => void;
  onToggleSelected: (noteId: string) => void;
  onSelectAll: () => void;
  onClearSelection: () => void;
  onCreate: () => void;
}) {
  const picked = notes.filter((note) => selected[note.id]).length;

  return (
    <RlPanel className="flex min-h-0 flex-col">
      <RlPanelHead
        eyebrow="Notes"
        meta={notes.length === 0 ? "none yet" : notes.length + (notes.length === 1 ? " note" : " notes")}
        action={
          <button type="button" onClick={onCreate} disabled={busy} className="rl-btn rl-btn--primary rl-btn--sm">
            <RlIcon name="plus" size={15} /> New note
          </button>
        }
      />

      {notes.length > 0 ? (
        <div className="flex items-center justify-between gap-2 border-t border-[color:var(--rl-border-parchment)] px-4 py-2">
          <span className="rl-card-meta">
            {picked === 0 ? "Pick the notes to build from" : picked + " picked"}
          </span>
          <button
            type="button"
            onClick={picked === notes.length ? onClearSelection : onSelectAll}
            className="rl-btn rl-btn--ghost rl-btn--sm"
          >
            {picked === notes.length ? "Clear" : "Select all"}
          </button>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto border-t border-[color:var(--rl-border-parchment)]">
        {loading ? (
          <p className="px-4 py-3 text-sm text-[color:var(--rl-text-on-parchment-muted)]">Loading notes…</p>
        ) : error ? (
          <div className="px-4 py-3">
            <Alert tone="error">{error}</Alert>
          </div>
        ) : notes.length === 0 ? (
          <div className="px-4 py-4">
            <EmptyState>
              No notes yet. A note is where you put what you have in mind — the world
              before the first session, or the ground you are preparing for the next one.
            </EmptyState>
          </div>
        ) : (
          <ul>
            {notes.map((note) => {
              const isActive = note.id === activeId;
              const isPicked = Boolean(selected[note.id]);
              return (
                <li
                  key={note.id}
                  className={
                    "flex items-start gap-3 border-b border-[color:var(--rl-border-parchment)] px-4 py-3 transition last:border-b-0 " +
                    (isActive ? "bg-[color:var(--rl-bg-parchment-sunk)]" : "")
                  }
                >
                  {/* Picking is separate from opening: a note can be read
                      without entering the next generation, and vice versa. */}
                  <input
                    type="checkbox"
                    checked={isPicked}
                    onChange={() => onToggleSelected(note.id)}
                    aria-label={"Build from " + note.title}
                    className="mt-1 h-4 w-4 shrink-0 accent-[color:var(--rl-accent-500)]"
                  />
                  <div className="min-w-0 flex-1">
                    <button
                      type="button"
                      onClick={() => onOpen(note.id)}
                      className="block w-full text-left"
                      aria-current={isActive ? "true" : undefined}
                    >
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="truncate text-sm font-semibold text-[color:var(--rl-text-on-parchment-primary)]">
                          {note.title}
                        </span>
                        {note.status === "ready" ? (
                          <RlTag tint="neutral">{NOTE_STATUS_LABELS.ready}</RlTag>
                        ) : null}
                      </span>
                      <span className="rl-card-meta mt-1 block">edited {stamp(note.updated_at)}</span>
                    </button>
                    {/* The body sits OUTSIDE that button on purpose. A note is
                        full of page tags, and a tag is there to be followed: a
                        link inside a button is not a thing HTML allows, so the
                        excerpt is read here and the title above is what opens
                        the note. The token stays as the DM typed it - this list
                        is their own shorthand, not a wiki body. */}
                    <div className="mt-0.5 line-clamp-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                      {note.body ? (
                        <LinkedText
                          text={note.body}
                          campaignId={campaignId}
                          index={linkIndex}
                          tokenLabel="token"
                        />
                      ) : (
                        "Empty note"
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </RlPanel>
  );
}

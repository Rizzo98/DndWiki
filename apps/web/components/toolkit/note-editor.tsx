// NoteEditor — one planning note, open for writing.
//
// A note is free text: a title, a body, and the DM's own bookkeeping status.
// There is no schema and no validation, because this is the one surface in the
// platform where a human writes prose from scratch rather than correcting a
// machine's reading of one.
//
// Two things make it more than a text box:
//
// - "@" opens the campaign's pages (PageLinkEditor): a note that says "the
//   party reaches @bree at dusk" is a note the DM can read back later without
//   wondering which Bree, and the token is the same one the wiki's own fields
//   use, so a note and a page describe a link the same way;
// - a preview renders the note the way the rest of the app reads it, with the
//   page names linked.
//
// The parent renders this with key={note.id} so switching notes remounts it:
// the alternative is an effect that resets four pieces of local state on every
// change of prop, which is the same thing done less reliably.

"use client";

import { useEffect, useState } from "react";
import { Alert, Field, TextInput } from "@/components/ui";
import { LinkedText, type LinkIndex } from "@/components/linked-text";
import { PageLinkEditor, type PageLinkSuggestion } from "@/components/page-link-editor";
import { RlButton, RlIcon, RlPanel, RlPanelHead, RlTag } from "@/components/ravenlore";
import { NOTE_STATUS_LABELS, type NoteStatus, type NoteSummary } from "@/lib/api";

export function NoteEditor({
  note,
  pages,
  linkIndex,
  campaignId,
  busy,
  error,
  onSave,
  onDelete,
}: {
  note: NoteSummary;
  /** The campaign's pages: the "@" autocomplete and the preview's links. */
  pages: PageLinkSuggestion[];
  linkIndex?: LinkIndex | null;
  campaignId: string;
  busy: boolean;
  error?: string | null;
  onSave: (patch: { title: string; body: string; status: NoteStatus }) => void;
  onDelete: () => void;
}) {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [status, setStatus] = useState<NoteStatus>(note.status);
  const [preview, setPreview] = useState(false);

  const dirty = title !== note.title || body !== note.body || status !== note.status;

  // Ctrl/Cmd+S saves, the way every editor the DM already uses does.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (dirty && !busy) onSave({ title, body, status });
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dirty, busy, title, body, status, onSave]);

  return (
    <RlPanel className="flex min-h-0 flex-col">
      <RlPanelHead
        eyebrow="Note"
        meta={
          dirty
            ? "unsaved changes"
            : "saved " + new Date(note.updated_at ?? Date.now()).toLocaleString()
        }
        action={
          <div className="flex items-center gap-2">
            <RlTag tint={status === "ready" ? "neutral" : "muted"}>
              {NOTE_STATUS_LABELS[status]}
            </RlTag>
            <button
              type="button"
              onClick={() => setPreview((v) => !v)}
              className="rl-btn rl-btn--ghost rl-btn--sm"
              aria-pressed={preview}
            >
              {preview ? "Write" : "Preview"}
            </button>
          </div>
        }
      />

      <div className="space-y-4 border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
        <Field label="Title">
          <TextInput
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="What is this note about?"
            maxLength={255}
          />
        </Field>

        {preview ? (
          <div>
            <span className="rl-eyebrow mb-1.5 block">Preview</span>
            <div className="rl-input-light min-h-[12rem] whitespace-pre-wrap text-sm leading-relaxed">
              {body.trim() ? (
                <LinkedText text={body} campaignId={campaignId} index={linkIndex} />
              ) : (
                <span className="text-[color:var(--rl-text-on-parchment-muted)]">
                  Nothing written yet.
                </span>
              )}
            </div>
          </div>
        ) : (
          <Field
            label="Note"
            hint="Type @ to search and link this campaign's wiki pages."
          >
            <PageLinkEditor
              rows={18}
              value={body}
              onChange={setBody}
              pages={pages}
              campaignId={campaignId}
              triggerChar="@"
              placeholder={"What you have in mind…\n\nKaelor rides for @bree before the thaw."}
            />
          </Field>
        )}

        {error ? <Alert tone="error">{error}</Alert> : null}

        <div className="flex flex-wrap items-center gap-2">
          <RlButton
            onClick={() => onSave({ title, body, status })}
            disabled={busy || !dirty}
          >
            {busy ? "Saving…" : "Save note"}
          </RlButton>
          <RlButton
            variant="outline"
            onClick={() => onSave({ title, body, status: status === "ready" ? "draft" : "ready" })}
            disabled={busy}
          >
            {status === "ready" ? "Back to draft" : "Mark as ready"}
          </RlButton>
          <button
            type="button"
            onClick={onDelete}
            disabled={busy}
            className="rl-btn rl-btn--danger rl-btn--sm ml-auto"
          >
            <RlIcon name="logout" size={15} /> Delete
          </button>
        </div>
      </div>
    </RlPanel>
  );
}

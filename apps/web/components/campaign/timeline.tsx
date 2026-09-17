// Timeline tab: in-world events; DM create/edit/approve. Every event has
// its own wiki page (kind="event") — clicking a row opens the page, and the
// create form drafts the page + the linked timeline entry together.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, TextArea, TextInput, fmtDate } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { wikiApi, type Campaign, type TimelineEvent } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export function TimelineTab({ campaign }: { campaign: Campaign }) {
  const { token, isDeveloper } = useAuth();
  const isDm = campaign.my_role === "dm";
  const { data: events, error, loading, reload } = useAsyncData<TimelineEvent[]>((t) => wikiApi.timeline(t, campaign.id), [campaign.id]);

  const [form, setForm] = useState({ title: "", in_world_date: "", summary: "", approved: true });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ in_world_date: "", summary: "", approved: true });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // DEBUG (developer accounts, realm role 'dev'): wipe every timeline entry
  // of the campaign at once — used while iterating on timeline generation so
  // stale LLM-proposed events don't pile up. Event pages stay in the wiki.
  async function deleteAllTimelineEvents() {
    if (!token) return;
    if (
      !window.confirm(
        "Delete ALL timeline events of this campaign?\n\nEvery timeline entry is removed permanently (the linked event pages stay in the wiki).",
      )
    ) {
      return;
    }
    if (!window.confirm("Really sure? This cannot be undone.")) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await wikiApi.deleteAllTimelineEvents(token, campaign.id);
      setNotice("Timeline reset: deleted " + res.deleted + " event(s).");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  /** Create the event page (kind="event") first, then the timeline entry
   * linked to it — every timeline event has its own wiki page. */
  async function createEvent(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    const title = form.title.trim();
    if (!title) {
      setFormError("Title is required (the event page is named after it).");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      // DM-authored events are published immediately (the timeline entry is
      // approved too); LLM-drafted events stay pending_review until the DM
      // approves page + entry.
      const page = await wikiApi.createPage(token, {
        campaign_id: campaign.id,
        kind: "event",
        title,
        status: "published",
        visibility: "public",
        content_json: {
          ...(form.summary.trim() ? { summary: form.summary.trim() } : {}),
          attributes: { in_world_date: form.in_world_date.trim() || null },
        },
      });
      await wikiApi.createTimelineEvent(token, {
        campaign_id: campaign.id,
        page_id: page.id,
        in_world_date: form.in_world_date.trim() || null,
        summary: form.summary.trim() || title,
        approved: form.approved,
      });
      setNotice("Event \"" + title + "\" created (its page is " + page.slug + ").");
      setForm({ title: "", in_world_date: "", summary: "", approved: true });
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function startEdit(ev: TimelineEvent) {
    setEditingId(ev.id);
    setEditForm({ in_world_date: ev.in_world_date ?? "", summary: ev.summary, approved: ev.approved });
  }

  async function saveEdit(ev: TimelineEvent) {
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      await wikiApi.updateTimelineEvent(token, ev.id, {
        in_world_date: editForm.in_world_date.trim() || null,
        summary: editForm.summary.trim() || undefined,
        approved: editForm.approved,
      });
      setEditingId(null);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      {isDm ? (
        <Card>
          <h2 className="rl-title mb-4 text-lg">Add timeline event</h2>
          <form onSubmit={createEvent} className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Title" hint="Becomes the event page name">
                <TextInput required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="The Siege of Fatumastra" />
              </Field>
              <Field label="In-world date">
                <TextInput value={form.in_world_date} onChange={(e) => setForm({ ...form, in_world_date: e.target.value })} placeholder="17 Ches 1492 DR" />
              </Field>
              <Field label="Approved">
                <select
                  className="w-full rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2 text-sm text-[color:var(--rl-text-on-parchment-primary)] outline-none focus:border-[color:var(--rl-accent-500)]"
                  value={form.approved ? "1" : "0"}
                  onChange={(e) => setForm({ ...form, approved: e.target.value === "1" })}
                >
                  <option value="1">Approved (visible to players)</option>
                  <option value="0">Not approved (DM only)</option>
                </select>
              </Field>
            </div>
            <Field label="Summary">
              <TextArea rows={2} required value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} placeholder="The horde breaks against the walls…" />
            </Field>
            <Button type="submit" disabled={busy}>Add event</Button>
          </form>
        </Card>
      ) : null}

      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="rl-title text-lg">Timeline</h2>
          {isDeveloper ? (
            <Button
              variant="danger"
              onClick={deleteAllTimelineEvents}
              disabled={busy}
              title="DEBUG: hard-delete every timeline event of this campaign"
            >
              Delete all timeline events
            </Button>
          ) : null}
        </div>
        {loading ? (
          <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">Loading timeline…</p>
        ) : error ? (
          <Alert tone="error">{error}</Alert>
        ) : events && events.length === 0 ? (
          <EmptyState>No timeline events yet.</EmptyState>
        ) : (
          <ol className="relative space-y-6 border-l border-[color:var(--rl-border-parchment)] pl-6">
            {events?.map((ev) => (
              <li key={ev.id} className="relative">
                <span className="absolute -left-[31px] top-1 h-3 w-3 rounded-full border-2 border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-accent-500)]" />
                {editingId === ev.id && isDm ? (
                  <div className="space-y-3 rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] p-4">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field label="In-world date">
                        <TextInput value={editForm.in_world_date} onChange={(e) => setEditForm({ ...editForm, in_world_date: e.target.value })} />
                      </Field>
                      <Field label="Approved">
                        <select
                          className="w-full rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2 text-sm text-[color:var(--rl-text-on-parchment-primary)] outline-none focus:border-[color:var(--rl-accent-500)]"
                          value={editForm.approved ? "1" : "0"}
                          onChange={(e) => setEditForm({ ...editForm, approved: e.target.value === "1" })}
                        >
                          <option value="1">Approved</option>
                          <option value="0">Not approved</option>
                        </select>
                      </Field>
                    </div>
                    <Field label="Summary">
                      <TextArea rows={2} value={editForm.summary} onChange={(e) => setEditForm({ ...editForm, summary: e.target.value })} />
                    </Field>
                    <div className="flex gap-2">
                      <Button onClick={() => saveEdit(ev)} disabled={busy}>Save</Button>
                      <Button variant="ghost" onClick={() => setEditingId(null)}>Cancel</Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      {ev.in_world_date ? <div className="text-xs font-semibold uppercase tracking-wide rl-text-accent">{ev.in_world_date}</div> : null}
                      {ev.page_id ? (
                        <Link
                          href={"/campaigns/" + campaign.id + "/pages/" + ev.page_id}
                          className="mt-1 block text-sm font-medium text-[color:var(--rl-text-on-parchment-primary)] hover:text-[color:var(--rl-accent-500)] hover:underline"
                        >
                          {ev.page_title ?? ev.summary}
                        </Link>
                      ) : (
                        <p className="mt-1 text-sm text-[color:var(--rl-text-on-parchment-primary)]">{ev.summary}</p>
                      )}
                      <p className="mt-1 text-xs text-[color:var(--rl-text-on-parchment-muted)]">added {fmtDate(ev.created_at)}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge tone={ev.approved ? "green" : "amber"}>{ev.approved ? "approved" : "pending"}</Badge>
                      {ev.page_id ? (
                        <Link
                          href={"/campaigns/" + campaign.id + "/pages/" + ev.page_id}
                          className="rounded-lg px-3 py-1.5 text-sm font-medium rl-text-accent transition hover:bg-[color:var(--rl-bg-parchment-sunk)] hover:underline"
                        >
                          Page
                        </Link>
                      ) : null}
                      {isDm ? (
                        <Button variant="ghost" onClick={() => startEdit(ev)}>Edit</Button>
                      ) : null}
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}

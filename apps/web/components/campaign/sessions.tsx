// Sessions tab: list + create sessions; each row links to the detail page
// where recordings are uploaded and the pipeline is tracked. A session can be
// DELETED until its wiki updates exist ('can_delete' from the API): once the
// pages are written they would outlive the session that produced them.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Alert, Button, Card, EmptyState, Field, SessionStatusBadge, TextInput, fmtDate, fmtDuration } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { sessionsApi, type Campaign, type Session } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export function SessionsTab({ campaign }: { campaign: Campaign }) {
  const { token } = useAuth();
  const isDm = campaign.my_role === "dm";
  const { data: sessions, error, loading, reload } = useAsyncData<Session[]>((t) => sessionsApi.list(t, campaign.id), [campaign.id]);
  const [form, setForm] = useState({ title: "", session_no: "" });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function createSession(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      const session = await sessionsApi.create(token, {
        campaign_id: campaign.id,
        title: form.title.trim() || null,
        session_no: form.session_no.trim() ? Number(form.session_no) : null,
      });
      setNotice(`Session created — status ${session.status}.`);
      setForm({ title: "", session_no: "" });
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSession(session: Session) {
    if (!token) return;
    const label = session.title ?? "Untitled session";
    if (
      !window.confirm(
        `Delete "${label}"?\n\nThe recording, the transcript and everything generated from this session are removed. This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      await sessionsApi.remove(token, session.id);
      setNotice(`Session "${label}" deleted.`);
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

      <Card>
        <h2 className="rl-title mb-4 text-lg">Record a session</h2>
        <form onSubmit={createSession} className="grid gap-3 sm:grid-cols-[1fr_8rem_auto]">
          <Field label="Title">
            <TextInput value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="The Siege of Vharn" />
          </Field>
          <Field label="Session #">
            <TextInput type="number" min={1} value={form.session_no} onChange={(e) => setForm({ ...form, session_no: e.target.value })} placeholder="3" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={busy}>Create</Button>
          </div>
        </form>
        <p className="mt-3 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
          After creating, open the session to upload the phone recording — the transcription pipeline starts automatically.
        </p>
      </Card>

      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <h2 className="rl-title text-lg">Sessions</h2>
          <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
            A session can be deleted until its wiki updates are generated.
          </p>
        </div>
        {loading ? (
          <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">Loading sessions…</p>
        ) : error ? (
          <Alert tone="error">{error}</Alert>
        ) : sessions && sessions.length === 0 ? (
          <EmptyState>No sessions yet — create the first one above.</EmptyState>
        ) : (
          <div className="divide-y divide-[color:var(--rl-border-parchment)]">
            {sessions?.map((s) => (
              <div key={s.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <Link
                  href={`/campaigns/${campaign.id}/sessions/${s.id}`}
                  className="min-w-0 flex-1 rounded-lg transition hover:bg-[color:var(--rl-bg-parchment-sunk)]"
                >
                  <div className="text-sm font-medium text-[color:var(--rl-text-on-parchment-primary)]">
                    {s.title ?? "Untitled session"}
                    {s.session_no ? <span className="ml-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">#{s.session_no}</span> : null}
                  </div>
                  <div className="mt-0.5 text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                    recorded {fmtDate(s.recorded_at)} · {fmtDuration(s.duration_sec)}
                  </div>
                </Link>
                <div className="flex items-center gap-3">
                  <SessionStatusBadge status={s.status} />
                  {isDm && s.can_delete ? (
                    <Button
                      variant="danger"
                      disabled={busy}
                      onClick={() => deleteSession(s)}
                      title="Delete this session (only possible until its wiki updates are generated)"
                    >
                      Delete
                    </Button>
                  ) : null}
                  <Link href={`/campaigns/${campaign.id}/sessions/${s.id}`} className="text-xs text-[color:var(--rl-text-on-parchment-muted)] hover:text-[color:var(--rl-text-on-parchment-primary)]">
                    →
                  </Link>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

// Sessions tab: list + create sessions; each row links to the detail page
// where recordings are uploaded and the pipeline is tracked.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Alert, Button, Card, EmptyState, Field, SessionStatusBadge, TextInput, fmtDate, fmtDuration } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { sessionsApi, type Campaign, type Session } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export function SessionsTab({ campaign }: { campaign: Campaign }) {
  const { token } = useAuth();
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

  return (
    <div className="space-y-6">
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Record a session</h2>
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
        <p className="mt-3 text-xs text-slate-500">
          After creating, open the session to upload the phone recording — the transcription pipeline starts automatically.
        </p>
      </Card>

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Sessions</h2>
        {loading ? (
          <p className="text-sm text-slate-400">Loading sessions…</p>
        ) : error ? (
          <Alert tone="error">{error}</Alert>
        ) : sessions && sessions.length === 0 ? (
          <EmptyState>No sessions yet — create the first one above.</EmptyState>
        ) : (
          <div className="divide-y divide-slate-800">
            {sessions?.map((s) => (
              <Link key={s.id} href={`/campaigns/${campaign.id}/sessions/${s.id}`} className="flex flex-wrap items-center justify-between gap-3 py-3 transition hover:bg-slate-800/40">
                <div>
                  <div className="text-sm font-medium text-slate-100">
                    {s.title ?? "Untitled session"}
                    {s.session_no ? <span className="ml-2 text-xs text-slate-500">#{s.session_no}</span> : null}
                  </div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    recorded {fmtDate(s.recorded_at)} · {fmtDuration(s.duration_sec)}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <SessionStatusBadge status={s.status} />
                  <span className="text-xs text-slate-600">→</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

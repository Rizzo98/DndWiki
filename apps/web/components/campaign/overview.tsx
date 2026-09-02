// Overview tab: campaign details + DM edit / archive / restore.

"use client";

import { FormEvent, useState } from "react";
import { Alert, Badge, Button, Card, Field, Select, TextArea, TextInput, fmtDate } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { CAMPAIGN_LANGUAGES, campaignLanguageLabel, campaignsApi, type Campaign } from "@/lib/api";
import { errMessage } from "@/lib/use-async";

export function OverviewTab({ campaign, onChanged }: { campaign: Campaign; onChanged: () => void }) {
  const { token } = useAuth();
  const isDm = campaign.my_role === "dm";
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ name: campaign.name, slug: campaign.slug, description: campaign.description ?? "", language: campaign.language });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await campaignsApi.update(token, campaign.id, {
        name: form.name.trim(),
        slug: form.slug.trim() || undefined,
        description: form.description.trim() || undefined,
        language: form.language,
      });
      setEditing(false);
      setNotice("Campaign updated.");
      onChanged();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleArchive() {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      if (campaign.status === "archived") await campaignsApi.restore(token, campaign.id);
      else await campaignsApi.archive(token, campaign.id);
      setNotice(campaign.status === "archived" ? "Campaign restored." : "Campaign archived (nothing is deleted).");
      onChanged();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {error ? <Alert tone="error">{error}</Alert> : null}

      <Card>
        <dl className="grid gap-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Name</dt>
            <dd className="mt-1 text-slate-100">{campaign.name}</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Slug</dt>
            <dd className="mt-1 font-mono text-sm text-slate-300">{campaign.slug}</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Status</dt>
            <dd className="mt-1">
              <Badge tone={campaign.status === "active" ? "green" : "slate"}>{campaign.status}</Badge>
            </dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Language</dt>
            <dd className="mt-1 text-sm text-slate-300">{campaignLanguageLabel(campaign.language)}</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">DM user id</dt>
            <dd className="mt-1 font-mono text-xs text-slate-400">{campaign.dm_user_id}</dd>
          </div>
          <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">Created</dt>
            <dd className="mt-1 text-sm text-slate-300">{fmtDate(campaign.created_at)}</dd>
          </div>
        </dl>
        {campaign.description ? (
          <p className="mt-4 border-t border-slate-800 pt-4 text-sm text-slate-300">{campaign.description}</p>
        ) : null}
      </Card>

      {isDm ? (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-semibold">DM actions</h2>
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => setEditing((v) => !v)}>{editing ? "Cancel" : "Edit details"}</Button>
              <Button variant={campaign.status === "archived" ? "secondary" : "danger"} onClick={toggleArchive} disabled={busy}>
                {campaign.status === "archived" ? "Restore campaign" : "Archive campaign"}
              </Button>
            </div>
          </div>

          {editing ? (
            <form onSubmit={save} className="mt-4 space-y-3">
              <Field label="Name">
                <TextInput required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </Field>
              <Field label="Slug" hint="lowercase-hyphenated">
                <TextInput value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
              </Field>
              <Field label="Description">
                <TextArea rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
              <Field label="Language" hint="The language all sessions will be played in.">
                <Select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
                  {CAMPAIGN_LANGUAGES.map((l) => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </Select>
              </Field>
              {error ? <Alert tone="error">{error}</Alert> : null}
              <Button type="submit" disabled={busy}>Save changes</Button>
            </form>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}

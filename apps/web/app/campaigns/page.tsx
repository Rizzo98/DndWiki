// /campaigns — list, create and join campaigns.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Badge, Button, Card, EmptyState, Field, TextInput, TextArea, Alert, fmtDate } from "@/components/ui";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, type Campaign } from "@/lib/api";
import { useAsyncData, errMessage } from "@/lib/use-async";

export default function CampaignsPage() {
  const { token, isDm } = useAuth();
  const { data: campaigns, error, loading, reload } = useAsyncData<Campaign[]>((t) => campaignsApi.list(t));
  const [form, setForm] = useState({ name: "", slug: "", description: "" });
  const [inviteToken, setInviteToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function createCampaign(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      const slug = form.slug.trim() || undefined;
      await campaignsApi.create(token, {
        name: form.name.trim(),
        ...(slug ? { slug } : {}),
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
      });
      setForm({ name: "", slug: "", description: "" });
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function acceptInvite(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      const campaign = await campaignsApi.acceptInvite(token, inviteToken.trim());
      setNotice(`Joined "${campaign.name}"!`);
      setInviteToken("");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthGate>
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold">Campaigns</h1>
        <p className="mt-1 text-sm text-slate-400">
          {isDm ? "You have the Dungeon Master realm role." : "Player account."} Campaigns you belong to appear below.
        </p>
      </div>

      {notice ? <Alert tone="success">{notice}</Alert> : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {loading ? (
            <Card><p className="text-sm text-slate-400">Loading campaigns…</p></Card>
          ) : error ? (
            <Alert tone="error">{error}</Alert>
          ) : campaigns && campaigns.length === 0 ? (
            <EmptyState>
              No campaigns yet. Create one (you become its Dungeon Master) or accept an invite on the right.
            </EmptyState>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              {campaigns?.map((c) => (
                <Link key={c.id} href={`/campaigns/${c.id}`}>
                  <Card className="h-full transition hover:border-slate-600">
                    <div className="flex items-start justify-between gap-2">
                      <h2 className="text-lg font-semibold text-slate-100">{c.name}</h2>
                      <Badge tone={c.my_role === "dm" ? "amber" : "blue"}>{c.my_role ?? "?"}</Badge>
                    </div>
                    <p className="mt-1 text-xs text-slate-500">slug: {c.slug}</p>
                    {c.description ? <p className="mt-2 line-clamp-2 text-sm text-slate-400">{c.description}</p> : null}
                    <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
                      <Badge tone={c.status === "active" ? "green" : "slate"}>{c.status}</Badge>
                      <span>created {fmtDate(c.created_at)}</span>
                    </div>
                  </Card>
                </Link>
              ))}
            </div>
          )}
        </div>

        <div className="space-y-6">
          <Card>
            <h2 className="mb-4 text-lg font-semibold">New campaign</h2>
            <form onSubmit={createCampaign} className="space-y-3">
              <Field label="Name">
                <TextInput required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="The Shattered Crown" />
              </Field>
              <Field label="Slug" hint="lowercase-hyphenated; optional (auto-derived)">
                <TextInput value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="the-shattered-crown" />
              </Field>
              <Field label="Description">
                <TextArea rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="A sandbox campaign in the Emerald Expanse…" />
              </Field>
              {formError ? <Alert tone="error">{formError}</Alert> : null}
              <Button type="submit" disabled={busy} className="w-full">Create (you become DM)</Button>
            </form>
          </Card>

          <Card>
            <h2 className="mb-4 text-lg font-semibold">Accept an invite</h2>
            <form onSubmit={acceptInvite} className="space-y-3">
              <Field label="Invite token">
                <TextInput required value={inviteToken} onChange={(e) => setInviteToken(e.target.value)} placeholder="paste the token from your DM" />
              </Field>
              {formError ? <Alert tone="error">{formError}</Alert> : null}
              <Button type="submit" variant="secondary" disabled={busy} className="w-full">Join campaign</Button>
            </form>
          </Card>
        </div>
      </div>
    </div>
    </AuthGate>
  );
}

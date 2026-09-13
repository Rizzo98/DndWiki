// /campaigns — the campaigns you belong to, plus invite redemption.
// Creating a campaign is a page of its own (/campaigns/new): this page only
// links to it.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Alert, Badge, Button, ButtonLink, Card, EmptyState, Field, TextInput, fmtDate } from "@/components/ui";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignLanguageLabel, campaignsApi, type Campaign } from "@/lib/api";
import { useAsyncData, errMessage } from "@/lib/use-async";

export default function CampaignsPage() {
  const { token, isDm } = useAuth();
  const { data: campaigns, error, loading, reload } = useAsyncData<Campaign[]>((t) => campaignsApi.list(t));
  const [inviteToken, setInviteToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);

  async function acceptInvite(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setInviteError(null);
    setNotice(null);
    try {
      const campaign = await campaignsApi.acceptInvite(token, inviteToken.trim());
      setNotice(`Joined "${campaign.name}"!`);
      setInviteToken("");
      reload();
    } catch (err) {
      setInviteError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthGate>
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold">Campaigns</h1>
          <p className="mt-1 text-sm text-slate-400">
            {isDm ? "You have the Dungeon Master realm role." : "Player account."} Campaigns you belong to appear below.
          </p>
        </div>
        <ButtonLink href="/campaigns/new">New campaign</ButtonLink>
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
              No campaigns yet. Create one with <strong className="text-slate-300">New campaign</strong> (you
              become its Dungeon Master) or accept an invite on the right.
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
                      <Badge tone="slate">{campaignLanguageLabel(c.language)}</Badge>
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
            <h2 className="mb-4 text-lg font-semibold">Accept an invite</h2>
            <form onSubmit={acceptInvite} className="space-y-3">
              <Field label="Invite token">
                <TextInput required value={inviteToken} onChange={(e) => setInviteToken(e.target.value)} placeholder="paste the token from your DM" />
              </Field>
              {inviteError ? <Alert tone="error">{inviteError}</Alert> : null}
              <Button type="submit" variant="secondary" disabled={busy} className="w-full">Join campaign</Button>
            </form>
          </Card>
        </div>
      </div>
    </div>
    </AuthGate>
  );
}

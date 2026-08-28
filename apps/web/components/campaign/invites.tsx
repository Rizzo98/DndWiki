// Invites tab: shareable join tokens (DM only tab).

"use client";

import { FormEvent, useState } from "react";
import { Alert, Button, Card, EmptyState, Field, TextInput, fmtDate } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { campaignsApi, type Campaign, type Invite } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export function InvitesTab({ campaign }: { campaign: Campaign }) {
  const { token } = useAuth();
  const { data: invites, error, loading, reload } = useAsyncData<Invite[]>((t) => campaignsApi.invites(t, campaign.id), [campaign.id]);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function createInvite(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      const invite = await campaignsApi.createInvite(token, campaign.id, { email: email.trim() || null, role: "player" });
      setNotice(`Invite created — token: ${invite.token}`);
      setEmail("");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(inviteId: string) {
    if (!token) return;
    if (!window.confirm("Revoke this invite? The link stops working.")) return;
    setBusy(true);
    try {
      await campaignsApi.revokeInvite(token, campaign.id, inviteId);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function copy(token: string) {
    void navigator.clipboard?.writeText(token);
    setNotice("Token copied to clipboard.");
  }

  return (
    <div className="space-y-6">
      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Create invite</h2>
        <form onSubmit={createInvite} className="flex flex-wrap items-end gap-3">
          <div className="min-w-64 flex-1">
            <Field label="Player email (optional)">
              <TextInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="player@example.com" />
            </Field>
          </div>
          <Button type="submit" disabled={busy}>Create invite</Button>
        </form>
        <p className="mt-3 text-xs text-slate-500">
          Share the invite token with your player; they redeem it from the Campaigns page (&ldquo;Accept an invite&rdquo;).
        </p>
      </Card>

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Pending invites</h2>
        {loading ? (
          <p className="text-sm text-slate-400">Loading invites…</p>
        ) : error ? (
          <Alert tone="error">{error}</Alert>
        ) : invites && invites.length === 0 ? (
          <EmptyState>No invites yet.</EmptyState>
        ) : (
          <div className="divide-y divide-slate-800">
            {invites?.map((inv) => (
              <div key={inv.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-100">{inv.email ?? "Any player"}</div>
                  <button
                    onClick={() => copy(inv.token)}
                    className="mt-0.5 block max-w-full truncate font-mono text-xs text-ember-400 hover:underline"
                    title="Click to copy"
                  >
                    {inv.token}
                  </button>
                  <div className="mt-1 text-xs text-slate-500">
                    created {fmtDate(inv.created_at)}
                    {inv.expires_at ? ` · expires ${fmtDate(inv.expires_at)}` : ""}
                    {inv.used_at ? ` · used ${fmtDate(inv.used_at)}` : ""}
                  </div>
                </div>
                {!inv.used_at ? (
                  <Button variant="ghost" onClick={() => revoke(inv.id)} disabled={busy} className="text-red-300 hover:bg-red-950/40">
                    Revoke
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

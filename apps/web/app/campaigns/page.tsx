// /campaigns — list, create and join campaigns.

"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { Badge, Button, Card, EmptyState, Field, TextInput, TextArea, Alert, fmtDate } from "@/components/ui";
import { UserPicker } from "@/components/campaign/members";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, type Campaign } from "@/lib/api";
import { useAsyncData, errMessage } from "@/lib/use-async";

interface MemberDraft {
  player_name: string;
  character_name: string;
  character_description: string;
  user_id: string | null;
}

const EMPTY_MEMBER: MemberDraft = {
  player_name: "",
  character_name: "",
  character_description: "",
  user_id: null,
};

function emptyDraft() {
  return { ...EMPTY_MEMBER };
}

export default function CampaignsPage() {
  const { token, isDm } = useAuth();
  const { data: campaigns, error, loading, reload } = useAsyncData<Campaign[]>((t) => campaignsApi.list(t));
  const [form, setForm] = useState({ name: "", slug: "", description: "" });
  const [members, setMembers] = useState<MemberDraft[]>([emptyDraft()]);
  const [inviteToken, setInviteToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  function setMember(index: number, patch: Partial<MemberDraft>) {
    setMembers((prev) => prev.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }

  const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

  async function createCampaign(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      const slug = form.slug.trim();
      if (slug && !SLUG_PATTERN.test(slug)) {
        setFormError("Slug must be lowercase-hyphenated (letters, digits and dashes only).");
        setBusy(false);
        return;
      }
      // Every non-empty row must be complete (player, character, description).
      const roster = members.filter(
        (m) => m.player_name.trim() || m.character_name.trim() || m.character_description.trim(),
      );
      if (roster.length === 0) {
        setFormError("Add at least one player (player name, character name and description).");
        setBusy(false);
        return;
      }
      for (let i = 0; i < roster.length; i++) {
        const m = roster[i];
        if (!m.player_name.trim() || !m.character_name.trim() || !m.character_description.trim()) {
          setFormError(`Player ${i + 1}: fill in the player name, character name and character description.`);
          setBusy(false);
          return;
        }
      }
      await campaignsApi.create(token, {
        name: form.name.trim(),
        ...(slug ? { slug } : {}),
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
        members: roster.map((m) => ({
          player_name: m.player_name.trim(),
          character_name: m.character_name.trim(),
          character_description: m.character_description.trim(),
          ...(m.user_id ? { user_id: m.user_id } : {}),
        })),
      });
      setForm({ name: "", slug: "", description: "" });
      setMembers([emptyDraft()]);
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
                <TextInput required maxLength={255} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="The Shattered Crown" />
              </Field>
              <Field label="Slug" hint="lowercase-hyphenated; optional (auto-derived)">
                <TextInput maxLength={64} value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="the-shattered-crown" />
              </Field>
              <Field label="Description">
                <TextArea rows={3} maxLength={2000} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="A sandbox campaign in the Emerald Expanse…" />
              </Field>

              <div className="border-t border-slate-800 pt-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                    Players (at least one)
                  </span>
                  <Button type="button" variant="ghost" onClick={() => setMembers((prev) => [...prev, emptyDraft()])}>
                    + Add player
                  </Button>
                </div>
                <div className="space-y-3">
                  {members.map((m, i) => (
                    <div key={i} className="space-y-2 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-semibold text-slate-500">Player {i + 1}</span>
                        {members.length > 1 ? (
                          <Button type="button" variant="ghost" onClick={() => setMembers((prev) => prev.filter((_, j) => j !== i))} className="px-2 py-1 text-xs text-red-300 hover:bg-red-950/40">
                            Remove
                          </Button>
                        ) : null}
                      </div>
                      <div className="grid gap-2 sm:grid-cols-2">
                        <Field label="Player name">
                          <TextInput required maxLength={255} value={m.player_name} onChange={(e) => setMember(i, { player_name: e.target.value })} placeholder="e.g. Alice" />
                        </Field>
                        <Field label="Character name">
                          <TextInput required maxLength={255} value={m.character_name} onChange={(e) => setMember(i, { character_name: e.target.value })} placeholder="e.g. Rowan" />
                        </Field>
                      </div>
                      <Field label="Character description" hint="Physical description — the transcript refiner uses it to recognize who is speaking.">
                        <TextArea required rows={2} maxLength={10000} value={m.character_description} onChange={(e) => setMember(i, { character_description: e.target.value })} placeholder="e.g. Tall half-elf rogue with silver hair and a scar over the left eye" />
                      </Field>
                      <Field label="Link to user (optional)">
                        <UserPicker token={token ?? ""} value={m.user_id} onChange={(userId) => setMember(i, { user_id: userId })} />
                      </Field>
                    </div>
                  ))}
                </div>
              </div>

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
// /profile — own profile (display name, avatar) + voiceprint enrollment.

"use client";

import { FormEvent, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, FileInput, Select, TextInput, fmtDate } from "@/components/ui";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, objectUrl, usersApi, voiceApi, type Campaign, type User, type VoiceProfile } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export default function ProfilePage() {
  const { token } = useAuth();
  const { data: me, error, loading, reload } = useAsyncData<User>((t) => usersApi.me(t));
  const { data: campaigns } = useAsyncData<Campaign[]>((t) => campaignsApi.list(t));
  const { data: profiles, reload: reloadProfiles } = useAsyncData<VoiceProfile[]>((t) => voiceApi.profiles(t));

  const [name, setName] = useState("");
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [voiceFile, setVoiceFile] = useState<File | null>(null);
  const [voiceCampaign, setVoiceCampaign] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function saveName(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      await usersApi.updateMe(token, name.trim());
      setNotice("Display name updated.");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function uploadAvatar(e: FormEvent) {
    e.preventDefault();
    if (!token || !avatarFile) return;
    setBusy(true);
    setFormError(null);
    try {
      await usersApi.uploadAvatar(token, avatarFile);
      setNotice("Avatar updated.");
      setAvatarFile(null);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function enroll(e: FormEvent) {
    e.preventDefault();
    if (!token || !voiceFile || !voiceCampaign) return;
    setBusy(true);
    setFormError(null);
    try {
      await voiceApi.enroll(token, voiceCampaign, voiceFile);
      setNotice("Voiceprint enrolled — the speaker pipeline can now recognize you.");
      setVoiceFile(null);
      setVoiceCampaign("");
      reloadProfiles();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  // campaign_id -> campaign name (voiceprints list); fall back to the raw id.
  const campaignName = (campaignId: string) =>
    campaigns?.find((c) => c.id === campaignId)?.name ?? campaignId;

  async function removeProfile(profileId: string) {
    if (!token) return;
    if (!window.confirm("Delete this voiceprint?")) return;
    setBusy(true);
    setFormError(null);
    try {
      await voiceApi.remove(token, profileId);
      reloadProfiles();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="text-sm text-slate-400">Loading profile…</p>;
  if (error || !me) return <Alert tone="error">{error ?? "Profile unavailable"}</Alert>;

  return (
    <AuthGate>
    <div className="space-y-8">
      <h1 className="text-3xl font-bold">Profile</h1>

      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <div className="flex items-center gap-4">
            {me.avatar_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={objectUrl(me.avatar_url) ?? undefined} alt="avatar" className="h-16 w-16 rounded-full object-cover ring-2 ring-slate-700" />
            ) : (
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-slate-800 text-2xl">🎭</div>
            )}
            <div>
              <div className="text-lg font-semibold">{me.display_name}</div>
              <div className="text-xs text-slate-500">{me.email ?? "no email"}</div>
              <div className="font-mono text-[11px] text-slate-600">{me.id}</div>
            </div>
          </div>

          <form onSubmit={saveName} className="mt-5 space-y-2 border-t border-slate-800 pt-5">
            <Field label="Display name">
              <TextInput required value={name || me.display_name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Button type="submit" disabled={busy}>Save name</Button>
          </form>

          <form onSubmit={uploadAvatar} className="mt-5 space-y-2 border-t border-slate-800 pt-5">
            <Field label="Avatar" hint="png/jpeg/webp, max 2 MB">
              <FileInput accept="image/png,image/jpeg,image/webp" onChange={(e) => setAvatarFile(e.target.files?.[0] ?? null)} />
            </Field>
            <Button type="submit" disabled={busy || !avatarFile}>Upload avatar</Button>
          </form>
        </Card>

        <Card>
          <h2 className="mb-4 text-lg font-semibold">Voiceprint enrollment</h2>
          <p className="mb-4 text-sm text-slate-400">
            Enroll a 10–30 s clip of yourself speaking so sessions are attributed to you automatically.
          </p>
          <form onSubmit={enroll} className="space-y-3">
            <Field label="Campaign">
              <Select value={voiceCampaign} onChange={(e) => setVoiceCampaign(e.target.value)} required>
                <option value="">Select a campaign…</option>
                {campaigns?.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </Select>
            </Field>
            <Field label="Sample clip">
              <FileInput accept="audio/*" onChange={(e) => setVoiceFile(e.target.files?.[0] ?? null)} />
            </Field>
            <Button type="submit" disabled={busy || !voiceFile || !voiceCampaign}>Enroll voiceprint</Button>
          </form>

          <h3 className="mb-2 mt-6 text-xs font-bold uppercase tracking-wider text-slate-500">My voiceprints</h3>
          {profiles && profiles.length === 0 ? (
            <EmptyState>No voiceprints enrolled.</EmptyState>
          ) : (
            <ul className="space-y-2">
              {profiles?.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 px-3 py-2 text-sm">
                  <div>
                    <div className="text-xs font-medium text-slate-300">{campaignName(p.campaign_id)}</div>
                    <div className="text-xs text-slate-500">v{p.embedding_version} · {fmtDate(p.created_at)}</div>
                    {p.sample_url ? (
                      <a href={objectUrl(p.sample_url) ?? "#"} target="_blank" rel="noreferrer" className="text-xs text-ember-400 hover:underline">
                        listen to sample
                      </a>
                    ) : null}
                  </div>
                  <Button variant="ghost" className="text-red-300 hover:bg-red-950/40" onClick={() => removeProfile(p.id)} disabled={busy}>
                    Delete
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
    </AuthGate>
  );
}

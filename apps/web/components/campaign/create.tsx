// Create-campaign form (rendered by the dedicated page at /campaigns/new):
// name, slug, description, session language and the starting roster. The
// caller becomes the Dungeon Master of the campaign they create.

"use client";

import { FormEvent, useState } from "react";
import { Alert, Button, Card, Field, Select, TextArea, TextInput } from "@/components/ui";
import { UserPicker } from "@/components/campaign/members";
import { useAuth } from "@/lib/auth";
import { CAMPAIGN_LANGUAGES, campaignsApi, type Campaign } from "@/lib/api";
import { errMessage } from "@/lib/use-async";

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

const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function emptyDraft(): MemberDraft {
  return { ...EMPTY_MEMBER };
}

export function CreateCampaignForm({ onCreated }: { onCreated: (campaign: Campaign) => void }) {
  const { token } = useAuth();
  const [form, setForm] = useState({ name: "", slug: "", description: "", language: "" });
  const [members, setMembers] = useState<MemberDraft[]>([emptyDraft()]);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function setMember(index: number, patch: Partial<MemberDraft>) {
    setMembers((prev) => prev.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }

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
      // The DM must choose the language the sessions will be in.
      if (!CAMPAIGN_LANGUAGES.some((l) => l.code === form.language)) {
        setFormError("Choose the language in which the sessions will be played.");
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
      const campaign = await campaignsApi.create(token, {
        name: form.name.trim(),
        ...(slug ? { slug } : {}),
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
        language: form.language,
        members: roster.map((m) => ({
          player_name: m.player_name.trim(),
          character_name: m.character_name.trim(),
          character_description: m.character_description.trim(),
          ...(m.user_id ? { user_id: m.user_id } : {}),
        })),
      });
      onCreated(campaign);
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
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
        <Field label="Language" hint="The language all sessions will be played in (required).">
          <Select required value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
            <option value="" disabled>Choose the session language…</option>
            {CAMPAIGN_LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>{l.label}</option>
            ))}
          </Select>
        </Field>

        <div className="border-t border-[color:var(--rl-border-parchment)] pt-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">
              Players (at least one)
            </span>
            <Button type="button" variant="ghost" onClick={() => setMembers((prev) => [...prev, emptyDraft()])}>
              + Add player
            </Button>
          </div>
          <div className="space-y-3">
            {members.map((m, i) => (
              <div key={i} className="space-y-2 rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-[color:var(--rl-text-on-parchment-muted)]">Player {i + 1}</span>
                  {members.length > 1 ? (
                    <Button type="button" variant="ghost" onClick={() => setMembers((prev) => prev.filter((_, j) => j !== i))} className="px-2 py-1 text-xs rl-text-villain hover:bg-[color:color-mix(in_srgb,var(--rl-cat-villain)_14%,transparent)]">
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
  );
}

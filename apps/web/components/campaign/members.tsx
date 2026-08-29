// Members tab: roster + DM management. A member is a player at the table —
// player name + character name — optionally linked to an existing platform
// user account. Unlinked players need no account; linked ones resolve their
// display name from user-service.

"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Field, TextInput, TextArea, fmtDate } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { campaignsApi, usersApi, type Campaign, type CampaignMember, type User } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

interface MemberRow extends CampaignMember {
  displayName?: string;
}

interface MemberPayload {
  player_name: string;
  character_name: string;
  character_description: string;
  user_id: string | null;
}

// ----------------------------------------------------------- user picker

export function UserPicker({
  token,
  value,
  onChange,
  disabled = false,
}: {
  token: string;
  value: string | null;
  onChange: (userId: string | null) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<User[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [label, setLabel] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Resolve the display name of the currently linked user.
  useEffect(() => {
    if (!value) {
      setLabel(null);
      return;
    }
    let cancelled = false;
    usersApi
      .get(token, value)
      .then((u) => {
        if (!cancelled) setLabel(u.display_name);
      })
      .catch(() => {
        if (!cancelled) setLabel(value);
      });
    return () => {
      cancelled = true;
    };
  }, [value, token]);

  // Debounced search-as-you-type over platform users.
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const q = query.trim();
    if (q.length < 2) {
      setResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    timer.current = setTimeout(async () => {
      try {
        const found = await usersApi.search(token, q);
        setResults(found);
        setOpen(true);
      } catch {
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query, token]);

  if (disabled) {
    return <TextInput value={value ?? ""} disabled placeholder="not editable" />;
  }

  if (value) {
    return (
      <div className="flex items-center gap-2">
        <span className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100">
          {label ?? "Loading…"}
        </span>
        <Button variant="ghost" type="button" onClick={() => onChange(null)}>
          Unlink
        </Button>
      </div>
    );
  }

  return (
    <div className="relative">
      <TextInput
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(false);
        }}
        onFocus={() => results.length > 0 && setOpen(true)}
        placeholder="Type to search users (2+ chars)"
      />
      {searching ? <p className="mt-1 text-xs text-slate-500">Searching…</p> : null}
      {open && results.length > 0 ? (
        <ul className="absolute z-10 mt-1 w-full overflow-hidden rounded-lg border border-slate-700 bg-slate-800 shadow-xl">
          {results.map((u) => (
            <li key={u.id}>
              <button
                type="button"
                className="block w-full px-3 py-2 text-left text-sm text-slate-100 hover:bg-slate-700"
                onClick={() => {
                  onChange(u.id);
                  setQuery("");
                  setResults([]);
                  setOpen(false);
                }}
              >
                {u.display_name}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------- member form

function MemberForm({
  initial,
  submitLabel,
  busy,
  error,
  lockUser,
  onSubmit,
}: {
  initial: MemberPayload;
  submitLabel: string;
  busy: boolean;
  error: string | null;
  lockUser?: boolean;
  onSubmit: (payload: MemberPayload) => void;
}) {
  const { token } = useAuth();
  const [form, setForm] = useState<MemberPayload>(initial);

  function submit(e: FormEvent) {
    e.preventDefault();
    onSubmit(form);
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Player name">
          <TextInput
            required
            maxLength={255}
            value={form.player_name}
            onChange={(e) => setForm({ ...form, player_name: e.target.value })}
            placeholder="e.g. Alice"
          />
        </Field>
        <Field label="Character name">
          <TextInput
            required
            maxLength={255}
            value={form.character_name}
            onChange={(e) => setForm({ ...form, character_name: e.target.value })}
            placeholder="e.g. Rowan, half-elf rogue"
          />
        </Field>
      </div>
      <Field
        label="Character description"
        hint="Physical description — the transcript refiner uses it to recognize who is speaking."
      >
        <TextArea
          required
          rows={2}
          maxLength={10000}
          value={form.character_description}
          onChange={(e) => setForm({ ...form, character_description: e.target.value })}
          placeholder="e.g. Tall half-elf rogue with silver hair and a scar over the left eye"
        />
      </Field>
      <Field
        label="Link to user (optional)"
        hint="Link this player to an existing platform account so they can use the app."
      >
        <UserPicker token={token ?? ""} value={form.user_id} disabled={lockUser} onChange={(userId) => setForm({ ...form, user_id: userId })} />
      </Field>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Button type="submit" disabled={busy}>
        {busy ? "Saving…" : submitLabel}
      </Button>
    </form>
  );
}

// ------------------------------------------------------------- members tab

export function MembersTab({ campaign }: { campaign: Campaign }) {
  const { token } = useAuth();
  const { data: members, error, loading, reload } = useAsyncData<CampaignMember[]>((t) => campaignsApi.members(t, campaign.id), [campaign.id]);
  const [rows, setRows] = useState<MemberRow[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Enrich linked members with their user display name (user-service).
  useEffect(() => {
    if (!members || !token) {
      setRows([]);
      return;
    }
    let cancelled = false;
    (async () => {
      const enriched = await Promise.all(
        members.map(async (m) => {
          if (!m.user_id) return { ...m, displayName: undefined };
          try {
            const u = await usersApi.get(token, m.user_id);
            return { ...m, displayName: u.display_name };
          } catch {
            return m;
          }
        }),
      );
      if (!cancelled) setRows(enriched);
    })();
    return () => {
      cancelled = true;
    };
  }, [members, token]);

  const editing = editingId ? rows.find((r) => r.id === editingId) ?? null : null;

  async function addMember(payload: MemberPayload) {
    if (!token) return;
    setBusy(true);
    setFormError(null);
    try {
      await campaignsApi.addMember(token, campaign.id, payload);
      setNotice("Member added.");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveMember(memberId: string, payload: MemberPayload) {
    if (!token) return;
    const prev = rows.find((r) => r.id === memberId);
    setBusy(true);
    setFormError(null);
    try {
      const body: { player_name?: string; character_name?: string; character_description?: string; user_id?: string; unlink_user?: boolean } = {};
      if (prev && payload.player_name !== prev.player_name) body.player_name = payload.player_name;
      if (prev && payload.character_name !== prev.character_name) body.character_name = payload.character_name;
      if (prev && payload.character_description !== (prev.character_description ?? "")) body.character_description = payload.character_description;
      if (!prev || payload.user_id !== prev.user_id) {
        if (payload.user_id === null) body.unlink_user = true;
        else body.user_id = payload.user_id;
      }
      await campaignsApi.updateMember(token, campaign.id, memberId, body);
      setNotice("Member updated.");
      setEditingId(null);
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function removeMember(memberId: string) {
    if (!token) return;
    if (!window.confirm("Remove this member from the campaign?")) return;
    setBusy(true);
    setFormError(null);
    try {
      await campaignsApi.removeMember(token, campaign.id, memberId);
      setNotice("Member removed.");
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
      {error ? <Alert tone="error">{error}</Alert> : null}

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Roster</h2>
        {loading ? (
          <p className="text-sm text-slate-400">Loading members…</p>
        ) : rows.length === 0 ? (
          <EmptyState>No members yet. Add your first player below.</EmptyState>
        ) : (
          <div className="divide-y divide-slate-800">
            {rows.map((m) => (
              <div key={m.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-slate-100">{m.player_name || "Unnamed player"}</span>
                    {m.role === "dm" ? <Badge tone="amber">dm</Badge> : <Badge tone="blue">player</Badge>}
                    {m.user_id ? (
                      <Badge tone="emerald">account: {m.displayName ?? m.user_id.slice(0, 8)}</Badge>
                    ) : (
                      <Badge tone="slate">no account</Badge>
                    )}
                  </div>
                  <div className="mt-0.5 text-xs text-slate-400">
                    {"Character: " + (m.character_name || "—")} · joined {fmtDate(m.joined_at)}
                  </div>
                  {m.character_description ? (
                    <div className="mt-1 max-w-xl text-xs italic text-slate-500 line-clamp-2">
                      {m.character_description}
                    </div>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="ghost" onClick={() => { setEditingId(m.id); setFormError(null); }}>
                    Edit
                  </Button>
                  {m.role !== "dm" ? (
                    <Button variant="ghost" onClick={() => removeMember(m.id)} disabled={busy} className="text-red-300 hover:bg-red-950/40">
                      Remove
                    </Button>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        {editing ? (
          <>
            <h2 className="mb-4 text-lg font-semibold">Edit {editing.player_name || "member"}</h2>
            <MemberForm
              key={editing.id}
              initial={{ player_name: editing.player_name, character_name: editing.character_name, character_description: editing.character_description ?? "", user_id: editing.user_id }}
              submitLabel="Save changes"
              busy={busy}
              error={formError}
              lockUser={editing.role === "dm"}
              onSubmit={(payload) => saveMember(editing.id, payload)}
            />
            <div className="mt-3">
              <Button variant="ghost" onClick={() => setEditingId(null)} disabled={busy}>
                Cancel
              </Button>
            </div>
          </>
        ) : (
          <>
            <h2 className="mb-4 text-lg font-semibold">Add player</h2>
            <MemberForm
              key="new"
              initial={{ player_name: "", character_name: "", character_description: "", user_id: null }}
              submitLabel="Add member"
              busy={busy}
              error={formError}
              onSubmit={addMember}
            />
          </>
        )}
      </Card>
    </div>
  );
}
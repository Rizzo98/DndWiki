// Relations card for the page detail: browse/search campaign pages (filtered
// by kind), pick one and attach it with a relation type — no raw page ids.

"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Card, Field, Select, TextInput } from "@/components/ui";
import { PAGE_KIND_LABELS, PAGE_KINDS, wikiApi, type PageRelation, type PageSummary, type WikiPageKind } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

/** Relation types offered out of the box (the API accepts any
 * [a-z][a-z0-9_]* value — "Custom…" covers the rest). */
const RELATION_TYPES: { value: string; label: string }[] = [
  { value: "appears_in", label: "Appears in" },
  { value: "member_of", label: "Member of" },
  { value: "allied_with", label: "Allied with" },
  { value: "led_by", label: "Led by" },
  { value: "owner", label: "Owner of" },
  { value: "possible_duplicate", label: "Possible duplicate" },
];

export function RelationsCard({
  campaignId,
  pageId,
  token,
  isDm,
}: {
  campaignId: string;
  pageId: string;
  token: string | null;
  isDm: boolean;
}) {
  const { data: relations, reload } = useAsyncData<PageRelation[]>(
    (t) => wikiApi.relations(t, pageId),
    [pageId],
  );

  // --- page picker: debounced search + kind filter ----------------------
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [kind, setKind] = useState<"" | WikiPageKind>("");
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  const { data: searchPages, loading: searchLoading } = useAsyncData<PageSummary[]>(
    (t) =>
      wikiApi.pages(t, campaignId, {
        ...(kind ? { kind } : {}),
        ...(debouncedQ ? { q: debouncedQ } : {}),
        limit: 20,
      }),
    [campaignId, kind, debouncedQ],
  );

  const [selected, setSelected] = useState<PageSummary | null>(null);
  const [relationType, setRelationType] = useState("appears_in");
  const [customType, setCustomType] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Never propose the page itself, an already-related page, or an archived one.
  const relatedIds = useMemo(
    () => new Set((relations ?? []).map((r) => r.related_page_id)),
    [relations],
  );
  const candidates = useMemo(
    () =>
      (searchPages ?? []).filter(
        (p) => p.id !== pageId && !relatedIds.has(p.id) && p.status !== "archived",
      ),
    [searchPages, pageId, relatedIds],
  );

  const effectiveType = relationType === "custom" ? customType.trim() : relationType;

  async function addRelation(e: FormEvent) {
    e.preventDefault();
    if (!token || !selected) return;
    if (!effectiveType) {
      setError("Choose or type a relation type.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await wikiApi.createRelation(token, pageId, selected.id, effectiveType);
      setNotice(`Relation added: ${selected.title} (${effectiveType}).`);
      setSelected(null);
      setQ("");
      setDebouncedQ("");
      setRelationType("appears_in");
      setCustomType("");
      reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function removeRelation(relationId: string) {
    if (!token) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await wikiApi.deleteRelation(token, pageId, relationId);
      setNotice("Relation removed.");
      reload();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Relations</h2>
        {relations && relations.length > 0 ? (
          <span className="text-xs text-slate-500">{relations.length} relation{relations.length === 1 ? "" : "s"}</span>
        ) : null}
      </div>

      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {error ? <Alert tone="error">{error}</Alert> : null}

      {isDm ? (
        <form onSubmit={addRelation} className="space-y-3 border-b border-slate-800 pb-4">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem]">
            <Field label="Search page">
              <TextInput value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by title…" />
            </Field>
            <Field label="Kind">
              <Select value={kind} onChange={(e) => setKind(e.target.value as WikiPageKind | "")}>
                <option value="">All kinds</option>
                {PAGE_KINDS.map((k) => (
                  <option key={k} value={k}>{PAGE_KIND_LABELS[k]}</option>
                ))}
              </Select>
            </Field>
          </div>

          <div>
            {searchLoading ? (
              <p className="text-xs text-slate-500">Loading pages…</p>
            ) : candidates.length === 0 ? (
              <p className="text-xs text-slate-500">
                {debouncedQ || kind
                  ? "No matching pages (already-related and archived pages are hidden)."
                  : "Type to search campaign pages, or pick a kind to browse."}
              </p>
            ) : (
              <ul className="max-h-48 divide-y divide-slate-800 overflow-y-auto rounded-lg border border-slate-800">
                {candidates.map((p) => {
                  const active = selected?.id === p.id;
                  return (
                    <li key={p.id}>
                      <button
                        type="button"
                        onClick={() => setSelected(p)}
                        className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition ${
                          active
                            ? "bg-amber-500/10 text-amber-200 ring-1 ring-inset ring-amber-500/40"
                            : "text-slate-200 hover:bg-slate-800/60"
                        }`}
                      >
                        <span className="truncate">{p.title}</span>
                        <Badge tone="slate">{PAGE_KIND_LABELS[p.kind] ?? p.kind}</Badge>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_10rem_auto]">
            <Field label="Related page">
              {selected ? (
                <div className="flex items-center justify-between gap-2 rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
                  <span className="truncate">{selected.title}</span>
                  <button
                    type="button"
                    onClick={() => setSelected(null)}
                    className="shrink-0 text-slate-400 hover:text-slate-200"
                    aria-label="Clear selection"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <div className="rounded-lg border border-slate-800 bg-slate-800/40 px-3 py-2 text-sm text-slate-500">
                  No page selected
                </div>
              )}
            </Field>
            <Field label="Relation type">
              <Select value={relationType} onChange={(e) => setRelationType(e.target.value)}>
                {RELATION_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
                <option value="custom">Custom…</option>
              </Select>
            </Field>
            <Button type="submit" disabled={busy || !selected}>
              Add relation
            </Button>
          </div>

          {relationType === "custom" ? (
            <Field label="Custom relation type" hint="lowercase letters, numbers and underscores">
              <TextInput value={customType} onChange={(e) => setCustomType(e.target.value)} placeholder="e.g. sworn_to" />
            </Field>
          ) : null}
        </form>
      ) : null}

      {relations && relations.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500">No relations yet.</p>
      ) : (
        <ul className="mt-4 space-y-2">
          {relations?.map((r) => (
            <li key={r.id} className="flex items-center justify-between gap-2 text-sm">
              <span className="text-slate-300">
                <span className="text-slate-500">{r.relation_type}</span> →{" "}
                <Link href={`/campaigns/${campaignId}/pages/${r.related_page_id}`} className="text-ember-400 hover:underline">
                  {r.related_title ?? r.related_page_id}
                </Link>
              </span>
              {isDm ? (
                <Button
                  variant="ghost"
                  className="text-red-300 hover:bg-red-950/40"
                  onClick={() => removeRelation(r.id)}
                  disabled={busy}
                >
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
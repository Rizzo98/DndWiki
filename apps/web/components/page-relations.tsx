// "Links between pages" for the page detail: what this page is linked to, and
// the DM's form to attach another one — browse/search campaign pages (filtered
// by kind), pick one and give the relation a type. Never a raw page id.
//
// The list speaks the same language as the change-set review on the session
// page (components/session/plan-relations-panel.tsx): a tinted type, the
// direction, and the target page with its category sigil.

"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Field, Select, TextInput } from "@/components/ui";
import { RlIcon, RlPanel, RlPanelHead, RlTag, rlTintVars, type RlTint } from "@/components/ravenlore";
import { PAGE_KIND_ICON, PAGE_KIND_TINT } from "@/lib/page-kinds";
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
  // The campaign's pages, so every target renders with its category sigil.
  const { data: allPages } = useAsyncData<PageSummary[]>(
    (t) => wikiApi.pages(t, campaignId, { limit: 500 }),
    [campaignId],
  );
  const kindById = useMemo(() => {
    const map = new Map<string, WikiPageKind>();
    for (const page of allPages ?? []) map.set(page.id, page.kind);
    return map;
  }, [allPages]);

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
    <RlPanel>
      <RlPanelHead
        eyebrow="Links between pages"
        meta={
          relations && relations.length > 0
            ? relations.length + (relations.length === 1 ? " link" : " links")
            : "none yet"
        }
      />
      {notice || error ? (
        <div className="space-y-2 px-4 pt-1">
          {notice ? <Alert tone="success">{notice}</Alert> : null}
          {error ? <Alert tone="error">{error}</Alert> : null}
        </div>
      ) : null}

      {isDm ? (
        <form onSubmit={addRelation} className="space-y-3 border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
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
              <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Loading pages…</p>
            ) : candidates.length === 0 ? (
              <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                {debouncedQ || kind
                  ? "No matching pages (already-related and archived pages are hidden)."
                  : "Type to search campaign pages, or pick a kind to browse."}
              </p>
            ) : (
              <ul className="max-h-48 divide-y divide-[color:var(--rl-border-parchment)] overflow-y-auto rounded-lg border border-[color:var(--rl-border-parchment)]">
                {candidates.map((p) => {
                  const active = selected?.id === p.id;
                  return (
                    <li key={p.id}>
                      <button
                        type="button"
                        onClick={() => setSelected(p)}
                        className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition ${
                          active
                            ? "rl-bg-item-soft rl-text-item rl-ring-item-inset"
                            : "text-[color:var(--rl-text-on-parchment-primary)] hover:bg-[color:var(--rl-bg-parchment-sunk)]"
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
                <div className="flex items-center justify-between gap-2 rounded-lg border rl-border-item rl-bg-item-soft px-3 py-2 text-sm rl-text-item">
                  <span className="truncate">{selected.title}</span>
                  <button
                    type="button"
                    onClick={() => setSelected(null)}
                    className="shrink-0 text-[color:var(--rl-text-on-parchment-muted)] hover:text-[color:var(--rl-text-on-parchment-primary)]"
                    aria-label="Clear selection"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <div className="rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2 text-sm text-[color:var(--rl-text-on-parchment-muted)]">
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
        <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
          This page is not linked to anything yet.
        </p>
      ) : (
        <RelationList
          relations={relations ?? []}
          campaignId={campaignId}
          kindOf={(id) => kindById.get(id) ?? null}
          canRemove={isDm}
          busy={busy}
          onRemove={removeRelation}
        />
      )}
    </RlPanel>
  );
}

/** The relations of a page, one row each — also rendered by the harness. */
export function RelationList({
  relations,
  campaignId,
  kindOf,
  canRemove,
  busy,
  onRemove,
}: {
  relations: PageRelation[];
  campaignId: string;
  /** Category of the page a relation points at (null when unknown). */
  kindOf: (pageId: string) => WikiPageKind | null;
  canRemove: boolean;
  busy: boolean;
  onRemove: (relationId: string) => void;
}) {
  return (
    <ul>
      {relations.map((relation) => {
        const kind = kindOf(relation.related_page_id);
        const tint: RlTint = kind ? PAGE_KIND_TINT[kind] ?? "muted" : "muted";
        return (
          <li
            key={relation.id}
            className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 border-t border-[color:var(--rl-border-parchment)] px-4 py-3"
          >
            <RlTag tint="accent">{relationLabel(relation.relation_type)}</RlTag>
            <RlIcon name="arrow" size={14} className="text-[color:var(--rl-text-on-parchment-muted)]" />
            <span className="rl-tint shrink-0" style={rlTintVars(tint)}>
              <RlIcon name={kind ? PAGE_KIND_ICON[kind] ?? "book" : "search"} size={14} />
            </span>
            <Link
              href={`/campaigns/${campaignId}/pages/${relation.related_page_id}`}
              className="truncate text-sm font-semibold rl-text-accent hover:underline"
            >
              {relation.related_title ?? relation.related_page_id}
            </Link>
            {canRemove ? (
              <Button
                variant="ghost"
                className="ml-auto"
                onClick={() => onRemove(relation.id)}
                disabled={busy}
              >
                Remove
              </Button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/** "possible_duplicate" -> "possible duplicate". */
function relationLabel(type: string): string {
  return type.replace(/_/g, " ");
}
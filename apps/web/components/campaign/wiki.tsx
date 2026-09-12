// Wiki tab: category menu (characters / locations / factions / items /
// quests) + DM page creation.

"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  PageStatusBadge,
  Select,
  TextArea,
  TextInput,
  VisibilityBadge,
  fmtDate,
  fmtPercent,
} from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  PAGE_KINDS,
  PAGE_KIND_LABELS,
  PAGE_STATUSES,
  VISIBILITIES,
  wikiApi,
  type Campaign,
  type PageSummary,
  type WikiPageKind,
  type WikiPageStatus,
  type WikiVisibility,
} from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

export function WikiTab({ campaign }: { campaign: Campaign }) {
  const { token, isDeveloper } = useAuth();
  const isDm = campaign.my_role === "dm";

  // The menu picks ONE category; status/search stay server-side. Pages are
  // fetched once per filter change and grouped by kind client-side so the
  // menu can show live counts and switching categories is instant.
  const [selectedKind, setSelectedKind] = useState<WikiPageKind | "">("");
  const [status, setStatus] = useState<"" | WikiPageStatus>("");
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q.trim()), 300);
    return () => window.clearTimeout(t);
  }, [q]);

  const { data: pages, error, loading, reload } = useAsyncData<PageSummary[]>(
    (t) =>
      wikiApi.pages(t, campaign.id, {
        ...(status ? { status: status as WikiPageStatus } : {}),
        ...(debouncedQ ? { q: debouncedQ } : {}),
        limit: 100,
      }),
    [campaign.id, status, debouncedQ],
  );

  const allPages = pages ?? [];
  const countByKind: Partial<Record<WikiPageKind, number>> = {};
  for (const p of allPages) {
    countByKind[p.kind] = (countByKind[p.kind] ?? 0) + 1;
  }
  const visiblePages = selectedKind ? allPages.filter((p) => p.kind === selectedKind) : allPages;

  const menuItems: { value: WikiPageKind | ""; label: string; count: number }[] = [
    { value: "", label: "All pages", count: allPages.length },
    ...PAGE_KINDS.map((k) => ({
      value: k,
      label: PAGE_KIND_LABELS[k],
      count: countByKind[k] ?? 0,
    })),
  ];

  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    kind: "character" as WikiPageKind,
    title: "",
    slug: "",
    status: "draft",
    visibility: "public",
    contentJson: "",
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // DEBUG (developer accounts): wipe every page of the campaign at once —
  // used while iterating on wiki generation so test drafts don't pile up.
  async function deleteAllPages() {
    if (!token) return;
    if (
      !window.confirm(
        "Delete ALL wiki pages of this campaign?\n\nEvery draft, published page, version and relation is removed permanently.",
      )
    ) {
      return;
    }
    if (!window.confirm("Really sure? This cannot be undone.")) return;
    setBusy(true);
    setFormError(null);
    try {
      const res = await wikiApi.deleteAllPages(token, campaign.id);
      setNotice("Wiki reset: deleted " + res.deleted + " page(s).");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function createPage(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setFormError(null);
    let contentJson: Record<string, unknown> = {};
    if (form.contentJson.trim()) {
      try {
        contentJson = JSON.parse(form.contentJson);
      } catch {
        setFormError("content_json must be valid JSON.");
        setBusy(false);
        return;
      }
    }
    try {
      await wikiApi.createPage(token, {
        campaign_id: campaign.id,
        kind: form.kind,
        title: form.title.trim(),
        ...(form.slug.trim() ? { slug: form.slug.trim() } : {}),
        status: form.status as WikiPageStatus,
        visibility: form.visibility as WikiVisibility,
        content_json: contentJson,
      });
      setNotice(`Page "${form.title.trim()}" created.`);
      setForm({ kind: "character", title: "", slug: "", status: "draft", visibility: "public", contentJson: "" });
      setShowCreate(false);
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

      <div className="grid gap-6 lg:grid-cols-[220px_1fr]">
        {/* ---------------------------------------------- category menu */}
        <nav aria-label="Wiki categories" className="h-fit space-y-1">
          {menuItems.map((item) => {
            const active = item.value === selectedKind;
            return (
              <button
                key={item.value || "all"}
                onClick={() => setSelectedKind(item.value)}
                aria-current={active ? "page" : undefined}
                className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active
                    ? "bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/40"
                    : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
                }`}
              >
                <span>{item.label}</span>
                <Badge tone={active ? "amber" : "slate"}>{item.count}</Badge>
              </button>
            );
          })}
        </nav>

        <div className="min-w-0 space-y-6">
          <Card>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Status">
                <Select
                  value={status}
                  onChange={(e) => setStatus(e.target.value as WikiPageStatus | "")}
                  className="w-44"
                >
                  <option value="">Any status</option>
                  {PAGE_STATUSES.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </Select>
              </Field>
              <div className="min-w-48 flex-1">
                <Field label="Search">
                  <TextInput value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search titles…" />
                </Field>
              </div>
              {isDm ? (
                <Button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Cancel" : "New page"}</Button>
              ) : null}
              {isDeveloper ? (
                <Button variant="danger" onClick={deleteAllPages} disabled={busy} title="DEBUG: hard-delete every page of this campaign">
                  Delete all pages
                </Button>
              ) : null}
            </div>

            {showCreate && isDm ? (
              <form onSubmit={createPage} className="mt-5 space-y-3 border-t border-slate-800 pt-5">
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Title">
                    <TextInput required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="Aragorn" />
                  </Field>
                  <Field label="Slug (optional)">
                    <TextInput value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="aragorn" />
                  </Field>
                  <Field label="Category">
                    <Select
                      value={form.kind}
                      onChange={(e) => setForm({ ...form, kind: e.target.value as WikiPageKind })}
                    >
                      {PAGE_KINDS.map((k) => (
                        <option key={k} value={k}>{PAGE_KIND_LABELS[k]}</option>
                      ))}
                    </Select>
                  </Field>
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="Status">
                      <Select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
                        {PAGE_STATUSES.map((s) => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </Select>
                    </Field>
                    <Field label="Visibility">
                      <Select value={form.visibility} onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
                        {VISIBILITIES.map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </Select>
                    </Field>
                  </div>
                </div>
                <Field
                  label="content_json"
                  hint='Optional JSON. Category attributes live under "attributes" — e.g. {"attributes": {"character_type": "npc", "race": "Elf", "gender": "Male"}} or {"attributes": {"location_type": "city", "region": "Costa"}}'
                >
                  <TextArea rows={5} className="font-mono text-xs" value={form.contentJson} onChange={(e) => setForm({ ...form, contentJson: e.target.value })} placeholder='{"physical_look": "…", "personality": "…"}' />
                </Field>
                {formError ? <Alert tone="error">{formError}</Alert> : null}
                <Button type="submit" disabled={busy}>Create page</Button>
              </form>
            ) : null}
          </Card>

          <Card>
            {loading ? (
              <p className="text-sm text-slate-400">Loading pages…</p>
            ) : error ? (
              <Alert tone="error">{error}</Alert>
            ) : visiblePages.length === 0 ? (
              <EmptyState>
                No pages here yet. Pages appear once a session summary is distilled and you
                confirm the proposed changes on the session page.
              </EmptyState>
            ) : (
              <div className="divide-y divide-slate-800">
                {visiblePages.map((p) => (
                  <Link key={p.id} href={`/campaigns/${campaign.id}/pages/${p.id}`} className="flex flex-wrap items-center justify-between gap-3 py-3 transition hover:bg-slate-800/40">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium text-slate-100">{p.title}</span>
                        <Badge tone="slate">{PAGE_KIND_LABELS[p.kind] ?? p.kind}</Badge>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {p.slug} · updated {fmtDate(p.updated_at)}
                        {p.confidence !== null && p.confidence !== undefined ? ` · confidence ${fmtPercent(p.confidence)}` : ""}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <PageStatusBadge status={p.status} />
                      <VisibilityBadge visibility={p.visibility} />
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

// Reusable wiki linking system: renders free text with entity names linked to
// their campaign pages.
//
// A LinkIndex maps page titles (and page slugs) to page ids; LinkedText scans
// the text and turns every exact, word-bounded occurrence of a known title
// into a clickable link to that page. Names are matched longest-first and
// case-insensitively, so "Locanda del Fumo Aspro" links even when written
// "locanda del fumo aspro", and a shorter name never shadows a longer one.
//
// The same index powers the manual "#slug" link syntax: a "#slug" token
// that matches a campaign page slug is rendered as a link to that page
// (displayed with the page's real title). The edit forms produce these
// tokens through PageLinkEditor's "#" autocomplete, but they can also be
// typed by hand in any free-text field.

"use client";

import Link from "next/link";
import { useMemo, type ReactNode } from "react";

export interface LinkTarget {
  id: string;
  title: string;
}

export interface LinkIndex {
  /** normalized page title -> page target */
  byName: Map<string, LinkTarget>;
  /** page slug -> page target (#slug references) */
  bySlug: Map<string, LinkTarget>;
  /** unique normalized names, longest first (drives the matching regex) */
  names: string[];
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Case/whitespace-insensitive key for a page title. */
export function normalizeName(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

/**
 * Build the lookup index from a campaign's page list (the lightweight
 * PageSummary[] shape). Archived pages are never linked.
 */
export function buildLinkIndex(
  pages: { id: string; title: string; slug?: string | null; status?: string | null }[],
): LinkIndex {
  const byName = new Map<string, LinkTarget>();
  const bySlug = new Map<string, LinkTarget>();
  for (const page of pages ?? []) {
    if (page.status === "archived") continue;
    const target: LinkTarget = { id: page.id, title: page.title };
    const key = normalizeName(page.title);
    if (key && !byName.has(key)) byName.set(key, target);
    if (page.slug && !bySlug.has(page.slug)) bySlug.set(page.slug, target);
  }
  return {
    byName,
    bySlug,
    names: Array.from(byName.keys()).sort((a, b) => b.length - a.length),
  };
}

// A "#page_slug" token (the future manual-linking syntax). Slug-safe charset.
const SLUG_SOURCE = "#[a-z0-9]+(?:-[a-z0-9]+)*";

function buildPattern(index: LinkIndex): RegExp | null {
  if (index.names.length === 0) return null;
  const names = index.names.map(escapeRegExp).join("|");
  // Word boundaries: a name only links when it is not part of a longer word,
  // and "#slug" only when '#' is not glued to a letter/number.
  return new RegExp(
    `(?<![\\p{L}\\p{N}])(${names}|${SLUG_SOURCE})(?![\\p{L}\\p{N}])`,
    "giu",
  );
}

/**
 * Render text with every known entity name (and "#slug" token) linked to its
 * page. No index or campaignId -> plain text.
 */
export function LinkedText({
  text,
  campaignId,
  index,
  currentPageId,
}: {
  text: string;
  campaignId?: string;
  index?: LinkIndex | null;
  /** The page being viewed: its own names are never linked to itself. */
  currentPageId?: string | null;
}) {
  const pattern = useMemo(() => (index ? buildPattern(index) : null), [index]);

  if (!index || !pattern || !campaignId) {
    return <>{text}</>;
  }

  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of Array.from(text.matchAll(pattern))) {
    const raw = match[0];
    const at = match.index ?? 0;
    if (at > last) nodes.push(text.slice(last, at));
    const isSlugLink = raw.startsWith("#");
    const target = isSlugLink
      ? index.bySlug.get(raw.slice(1))
      : index.byName.get(normalizeName(raw));
    if (target && target.id !== currentPageId) {
      // "#slug" tokens display as the page's real title; plain names keep
      // the text as written.
      nodes.push(
        <Link
          key={at}
          href={`/campaigns/${campaignId}/pages/${target.id}`}
          className="rl-text-accent hover:underline"
        >
          {isSlugLink ? target.title : raw}
        </Link>
      );
    } else {
      nodes.push(raw);
    }
    last = at + raw.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return <>{nodes}</>;
}
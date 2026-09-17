// PageLinkEditor — textarea with a "#" autocomplete for wiki page links.
//
// While editing prose (descriptions, facts, history...), typing "#" opens a
// dropdown of the campaign's pages. Picking one inserts the page's slug as a
// "#slug" token (e.g. "#locanda-del-fumo-aspro"); the renderer
// (components/linked-text.tsx) turns that token into a link to the page.
//
// The token is a single word: "#" followed by letters/digits/hyphens, so the
// autocomplete ends when the user types a space. Matching is case-insensitive
// against page titles and slugs (prefix preferred), so "#locanda" finds
// "Locanda del Fumo Aspro".

"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { PAGE_KIND_LABELS, type WikiPageKind } from "@/lib/api";

export interface PageLinkSuggestion {
  id: string;
  title: string;
  slug?: string | null;
  kind?: WikiPageKind | string | null;
  status?: string | null;
}

interface Trigger {
  /** Index of the "#" that started the token. */
  start: number;
  /** Text typed after "#" (single word, may be empty). */
  query: string;
}

const MAX_SUGGESTIONS = 8;
// Unicode word chars (the "u" flag needs new RegExp — the tsconfig target is
// below ES6, which rejects the /u literal flag).
const WORD = new RegExp("[\\p{L}\\p{N}]", "u");

function slugify(title: string): string {
  return (
    title
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "page"
  );
}

/** The "#query" token right before the caret, or null when not typing a link.
 * Mirrors the renderer: "#" must not be glued to a letter/number, and the
 * query is a single word (no whitespace). */
function findTrigger(value: string, caret: number): Trigger | null {
  const prefix = value.slice(0, caret);
  const hash = prefix.lastIndexOf("#");
  if (hash === -1) return null;
  if (hash > 0 && WORD.test(prefix[hash - 1])) return null;
  const query = prefix.slice(hash + 1);
  if (/\s/.test(query)) return null;
  return { start: hash, query };
}

/** Rank pages for the query: prefix matches on title/slug first, then
 * substring matches; stable by title. The current page is never suggested. */
function rankSuggestions(
  pages: PageLinkSuggestion[],
  query: string,
  excludeId?: string | null,
): PageLinkSuggestion[] {
  const q = query.trim().toLowerCase();
  const scored = (pages ?? [])
    .filter((p) => p.id !== excludeId && p.status !== "archived")
    .map((page) => {
      const title = page.title.toLowerCase();
      const slug = (page.slug ?? "").toLowerCase();
      let score = -1;
      if (!q) {
        score = 0;
      } else if (title.startsWith(q)) {
        score = 0;
      } else if (slug.startsWith(q)) {
        score = 1;
      } else if (title.includes(q)) {
        score = 2;
      } else if (slug.includes(q)) {
        score = 3;
      }
      return { page, score };
    })
    .filter((x) => x.score >= 0)
    .sort(
      (a, b) =>
        a.score - b.score ||
        a.page.title.localeCompare(b.page.title),
    );
  return scored.slice(0, MAX_SUGGESTIONS).map((x) => x.page);
}

function kindLabel(kind?: string | null): string {
  if (!kind) return "";
  return (PAGE_KIND_LABELS[kind as WikiPageKind] ?? kind).toLowerCase();
}

export function PageLinkEditor({
  value,
  onChange,
  pages,
  excludePageId,
  placeholder,
  rows = 4,
  className = "",
}: {
  value: string;
  onChange: (value: string) => void;
  /** Campaign pages to suggest (PageSummary-like). */
  pages: PageLinkSuggestion[];
  /** The page being edited: never suggested (no self-links). */
  excludePageId?: string | null;
  placeholder?: string;
  rows?: number;
  className?: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [trigger, setTrigger] = useState<Trigger | null>(null);
  const [highlight, setHighlight] = useState(0);
  const caretRef = useRef(0);

  const suggestions = useMemo(
    () => rankSuggestions(pages, trigger?.query ?? "", excludePageId),
    [pages, trigger, excludePageId],
  );

  // Reset the highlighted row when the suggestion list or query changes.
  useEffect(() => {
    setHighlight(0);
  }, [trigger?.query, suggestions.length]);

  function handleChange(next: string, caret: number) {
    caretRef.current = caret;
    setTrigger(findTrigger(next, caret));
    onChange(next);
  }

  function select(page: PageLinkSuggestion) {
    if (!trigger) return;
    const slug = page.slug || slugify(page.title);
    const before = value.slice(0, trigger.start);
    const after = value.slice(caretRef.current);
    const next = before + "#" + slug + after;
    onChange(next);
    setTrigger(null);
    // Put the caret right after the inserted token and keep focus.
    requestAnimationFrame(() => {
      const el = ref.current;
      if (!el) return;
      const pos = before.length + 1 + slug.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (!trigger || suggestions.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => (h + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => (h - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      select(suggestions[highlight]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setTrigger(null);
    }
  }

  return (
    <div className={"relative " + className}>
      <textarea
        ref={ref}
        rows={rows}
        value={value}
        placeholder={placeholder}
        onChange={(e) => handleChange(e.target.value, e.target.selectionStart)}
        onSelect={(e) => {
          caretRef.current = e.currentTarget.selectionStart;
          setTrigger(findTrigger(e.currentTarget.value, e.currentTarget.selectionStart));
        }}
        onClick={(e) => {
          caretRef.current = e.currentTarget.selectionStart;
          setTrigger(findTrigger(e.currentTarget.value, e.currentTarget.selectionStart));
        }}
        onKeyUp={(e) => {
          caretRef.current = e.currentTarget.selectionStart;
        }}
        onKeyDown={onKeyDown}
        onBlur={() => setTrigger(null)}
        className="w-full rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2 text-sm text-[color:var(--rl-text-on-parchment-primary)] placeholder:text-[color:var(--rl-text-on-parchment-muted)] outline-none focus:border-[color:var(--rl-accent-500)]"
      />
      {trigger ? (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] shadow-xl">
          {suggestions.length === 0 ? (
            <div className="px-3 py-2 text-xs text-[color:var(--rl-text-on-parchment-muted)]">No matching pages</div>
          ) : (
            suggestions.map((page, i) => (
              <button
                key={page.id}
                type="button"
                onMouseDown={(e) => e.preventDefault()} // keep textarea focus
                onMouseEnter={() => setHighlight(i)}
                onClick={() => select(page)}
                className={
                  "flex w-full items-baseline justify-between gap-3 px-3 py-2 text-left text-sm transition " +
                  (i === highlight ? "rl-bg-accent-soft rl-text-accent" : "text-[color:var(--rl-text-on-parchment-primary)] hover:bg-[color:var(--rl-bg-parchment-sunk)]")
                }
              >
                <span className="truncate">{page.title}</span>
                <span className="shrink-0 text-[10px] uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">
                  {kindLabel(page.kind)}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

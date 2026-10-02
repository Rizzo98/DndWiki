// PageLinkEditor — a textarea with an autocomplete that links wiki pages.
//
// While editing prose (descriptions, facts, history...), typing "#" opens a
// popover of the campaign's pages. Picking one inserts the page's slug as a
// "#slug" token (e.g. "#locanda-del-fumo-aspro"); the renderer
// (components/linked-text.tsx) turns that token into a link to the page.
//
// The token is a single word: "#" followed by letters/digits/hyphens, so the
// autocomplete ends when the user types a space. Matching is case-insensitive
// against page titles and slugs (prefix preferred), so "#locanda" finds
// "Locanda del Fumo Aspro".
//
// The trigger character is a parameter. A DM's planning notes (the toolkit's
// 'plan' tool) reference a page with "@", which is the gesture people already
// make to name something, while "#" stays the in-page syntax of the wiki's own
// fields. Both produce the same kind of token and are rendered by the same
// code, so there is one autocomplete and one linking rule, not two.
//
// Three things make the popover usable rather than merely present:
//
// - It opens at the caret, not under the field. A textarea exposes no API for
//   "where is the caret in pixels", so caretBox() below lays the text out
//   again in a hidden mirror div built from the textarea's own computed style
//   and asks where the next character lands. The popover then flips above the
//   line when there is no room below it, and stays inside the viewport
//   horizontally;
// - It holds a search field of its own. The token grammar allows a single
//   word, which is not enough to search with: "locanda fumo" can be typed in
//   the field and used to pick a page, while the note itself keeps only the
//   token that gets replaced. Until the DM edits the field it mirrors what was
//   typed after the trigger, so the common case needs no extra click;
// - It lists every match in a scrollable area. A row carries the page's title
//   with the matched part picked out, the tag that will be written into the note
//   (in the link colour, because that is what it is, and itself a link to the
//   page in another tab: looking at a page is part of choosing it), and the
//   page's kind. "Which Bree?" is the question a link is answering.

"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import Link from "next/link";
import { RlIcon } from "@/components/ravenlore";
import { PAGE_KIND_LABELS, type WikiPageKind } from "@/lib/api";

export interface PageLinkSuggestion {
  id: string;
  title: string;
  slug?: string | null;
  kind?: WikiPageKind | string | null;
  status?: string | null;
}

/** The character that opens the page autocomplete. "@" is the toolkit's. */
export type PageLinkTrigger = "#" | "@";

interface Trigger {
  /** Index of the trigger character that started the token. */
  start: number;
  /** Caret position when the token was last read: where the token ends. */
  end: number;
  /** Text typed after the trigger (single word, may be empty). */
  query: string;
}

/** Where the popover sits inside the wrapper, in pixels. */
interface Anchor {
  left: number;
  top: number;
  width: number;
  /** Height of the scrollable list, capped to what the viewport can show. */
  listMaxHeight: number;
}

/** Matches drawn at once. Every match is ranked; this only bounds the DOM. */
const MAX_ROWS = 50;
const PANEL_MIN_W = 300;
const PANEL_MAX_W = 460;
const PANEL_LIST_MAX_H = 300;
const PANEL_GAP = 6;
const VIEWPORT_MARGIN = 8;

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

/** The "<trigger>query" token right before the caret, or null when not typing
 * a link. Mirrors the renderer: the trigger must not be glued to a
 * letter/number (so an email address never opens the popover), and the query
 * is a single word (no whitespace), which is what ends the token. */
function findTrigger(value: string, caret: number, trigger: PageLinkTrigger): Trigger | null {
  const prefix = value.slice(0, caret);
  const at = prefix.lastIndexOf(trigger);
  if (at === -1) return null;
  if (at > 0 && WORD.test(prefix[at - 1])) return null;
  const query = prefix.slice(at + 1);
  if (/\s/.test(query)) return null;
  return { start: at, end: caret, query };
}

/** Rank pages for the query — all of them, unsliced, so the popover can say
 * how many matched. Prefix matches on title/slug come first, then substring
 * matches; stable by title. The current page is never suggested. */
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
    .sort((a, b) => a.score - b.score || a.page.title.localeCompare(b.page.title));
  return scored.map((x) => x.page);
}

function kindLabel(kind?: string | null): string {
  if (!kind) return "";
  return (PAGE_KIND_LABELS[kind as WikiPageKind] ?? kind).toLowerCase();
}

/** The title with the matched part picked out, so a row says why it is there.
 * Only the first match is marked: enough to explain the row, without turning
 * it into a puzzle. */
function markMatch(title: string, query: string): ReactNode {
  const q = query.trim().toLowerCase();
  if (!q) return title;
  const at = title.toLowerCase().indexOf(q);
  if (at === -1) return title;
  return (
    <>
      {title.slice(0, at)}
      <span className="font-semibold text-[color:var(--rl-accent-500)]">
        {title.slice(at, at + q.length)}
      </span>
      {title.slice(at + q.length)}
    </>
  );
}

// The properties the mirror must copy for its text to wrap exactly where the
// textarea's does. Padding is per-side because the mirror's own width is
// computed from the textarea's content box.
const MIRROR_PROPS = [
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "font-family",
  "font-size",
  "font-style",
  "font-weight",
  "font-variant",
  "font-stretch",
  "line-height",
  "letter-spacing",
  "word-spacing",
  "text-align",
  "text-indent",
  "text-transform",
  "tab-size",
];

interface CaretBox {
  /** Pixels from the textarea's border box (already includes its border). */
  top: number;
  left: number;
  height: number;
}

/** Where the caret sits inside the textarea, measured by laying the text out
 * again. The mirror is attached while it is measured — offsets are zero for a
 * detached element — and removed in the same frame, so it never paints. */
function caretBox(el: HTMLTextAreaElement, index: number): CaretBox {
  const computed = window.getComputedStyle(el);
  const mirror = document.createElement("div");
  for (const prop of MIRROR_PROPS) mirror.style.setProperty(prop, computed.getPropertyValue(prop));

  const padLeft = parseFloat(computed.paddingLeft) || 0;
  const padRight = parseFloat(computed.paddingRight) || 0;
  const borderTop = parseFloat(computed.borderTopWidth) || 0;
  const borderLeft = parseFloat(computed.borderLeftWidth) || 0;

  mirror.style.position = "absolute";
  mirror.style.top = "0";
  mirror.style.left = "0";
  mirror.style.height = "auto";
  mirror.style.visibility = "hidden";
  mirror.style.pointerEvents = "none";
  mirror.style.boxSizing = "content-box";
  mirror.style.border = "0";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  // clientWidth is the padding box, so the width the textarea wraps at is
  // that minus both paddings.
  mirror.style.width = Math.max(0, el.clientWidth - padLeft - padRight) + "px";

  mirror.textContent = el.value.slice(0, index);
  const marker = document.createElement("span");
  // At the very end of the text there is no character to measure, so a dot
  // stands in for one: its left edge is the caret, its height is the line.
  marker.textContent = el.value.charAt(index) || ".";
  mirror.appendChild(marker);
  document.body.appendChild(mirror);

  const box: CaretBox = {
    top: borderTop + marker.offsetTop,
    left: borderLeft + marker.offsetLeft,
    height: marker.offsetHeight || parseFloat(computed.lineHeight) || 20,
  };
  document.body.removeChild(mirror);
  return box;
}

export function PageLinkEditor({
  value,
  onChange,
  pages,
  campaignId,
  excludePageId,
  placeholder,
  rows = 4,
  className = "",
  triggerChar = "#",
}: {
  value: string;
  onChange: (value: string) => void;
  /** Campaign pages to suggest (PageSummary-like). */
  pages: PageLinkSuggestion[];
  /** The campaign the pages belong to. Given it, each row's tag is a link to
   * that page (in another tab); without it the tag is only a label. */
  campaignId?: string;
  /** The page being edited: never suggested (no self-links). */
  excludePageId?: string | null;
  placeholder?: string;
  rows?: number;
  className?: string;
  /** Character that opens the popover: "#" (wiki fields) or "@" (notes). */
  triggerChar?: PageLinkTrigger;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  const listId = useId();

  const [trigger, setTrigger] = useState<Trigger | null>(null);
  /** The popover's own search text. null = follow the token in the note. */
  const [search, setSearch] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [anchor, setAnchor] = useState<Anchor | null>(null);

  const query = search ?? trigger?.query ?? "";
  const matches = useMemo(
    () => rankSuggestions(pages, query, excludePageId),
    [pages, query, excludePageId],
  );
  const shown = matches.slice(0, MAX_ROWS);

  const close = useCallback(() => {
    setTrigger(null);
    setSearch(null);
  }, []);

  // A new query means a new list: start at the top of it.
  useEffect(() => {
    setHighlight(0);
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [query]);

  // Keep the highlighted row inside the list by scrolling the list only: the
  // popover can hang past the bottom of the viewport, and scrollIntoView would
  // drag the whole document down to reach it.
  useEffect(() => {
    const list = listRef.current;
    const row = rowRefs.current[highlight];
    if (!list || !row) return;
    const top = row.offsetTop;
    const bottom = top + row.offsetHeight;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }, [highlight, shown.length]);

  const place = useCallback(() => {
    const el = ref.current;
    const wrap = wrapRef.current;
    if (!el || !wrap || !trigger) return;

    const box = caretBox(el, trigger.end);
    const wrapRect = wrap.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const room = vw - VIEWPORT_MARGIN * 2;

    const width = Math.max(
      Math.min(PANEL_MIN_W, room),
      Math.min(PANEL_MAX_W, el.clientWidth || PANEL_MAX_W, room),
    );
    // Leave room for the search field and the footer, and never let the list
    // run off the bottom of the screen.
    const listMaxHeight = Math.max(140, Math.min(PANEL_LIST_MAX_H, vh - 220));

    // Both the textarea and the popover are positioned against the wrapper, so
    // everything here is in the wrapper's coordinates. The textarea's own
    // scroll moves the caret inside it; subtracting it is what keeps the
    // popover on the caret while the field scrolls.
    const caretTop = el.offsetTop + box.top - el.scrollTop;
    const caretLeft = el.offsetLeft + box.left - el.scrollLeft;

    let left = caretLeft;
    const absLeft = wrapRect.left + left;
    if (absLeft + width > vw - VIEWPORT_MARGIN) left -= absLeft + width - (vw - VIEWPORT_MARGIN);
    if (wrapRect.left + left < VIEWPORT_MARGIN) left = VIEWPORT_MARGIN - wrapRect.left;

    const panelHeight = panelRef.current?.offsetHeight ?? 0;
    const caretTopAbs = wrapRect.top + caretTop;
    const below = caretTop + box.height + PANEL_GAP;
    const fitsBelow = caretTopAbs + box.height + PANEL_GAP + panelHeight <= vh - VIEWPORT_MARGIN;
    const fitsAbove = caretTopAbs - PANEL_GAP - panelHeight >= VIEWPORT_MARGIN;
    let top = below;
    if (!fitsBelow) {
      if (fitsAbove) top = caretTop - PANEL_GAP - panelHeight;
      // Neither side has room (a short window): keep the popover on screen and
      // let it cover the line below the caret.
      else if (panelHeight > 0) top = Math.min(below, vh - VIEWPORT_MARGIN - panelHeight - wrapRect.top);
    }

    setAnchor((prev) =>
      prev &&
      prev.left === left &&
      prev.top === top &&
      prev.width === width &&
      prev.listMaxHeight === listMaxHeight
        ? prev
        : { left, top, width, listMaxHeight },
    );
  }, [trigger]);

  // Positioning needs the DOM — the caret's pixels and the panel's height — so
  // it runs after every commit while the popover is open. place() hands back
  // the previous anchor object when nothing moved, which is what stops this
  // from looping.
  useLayoutEffect(() => {
    if (trigger) place();
    else setAnchor(null);
  });

  // The caret moves when the field scrolls, and the viewport can be resized
  // under the popover.
  useEffect(() => {
    if (!trigger) return;
    const el = ref.current;
    const onMove = () => place();
    el?.addEventListener("scroll", onMove);
    window.addEventListener("resize", onMove);
    return () => {
      el?.removeEventListener("scroll", onMove);
      window.removeEventListener("resize", onMove);
    };
  }, [trigger, place]);

  function handleChange(next: string, caret: number) {
    // Typing in the note retakes the query from the search field.
    setSearch(null);
    setTrigger(findTrigger(next, caret, triggerChar));
    onChange(next);
  }

  /** A click in the textarea: read the token the caret now sits in.
   *
   * Only a click comes here, and that is deliberate. There used to be an onSelect
   * handler too, and it reopened the popover on the tag select() had just
   * written: the list blinked shut and straight back open. Reproduced in a
   * browser against this component - React's select plugin fires onSelect for a
   * collapsed caret, and it fires it on the KEYUP of the very key that inserted
   * the tag, which lands after the frame that restores the caret. The guard that
   * frame clears is therefore already down, and the tag is read straight back.
   * Nothing a person does selects text in the note without clicking, so the
   * click is the whole signal needed here; arrow keys are handled in
   * syncTokenEnd, which never opens on the Enter that just inserted. */
  function syncFromCaret(el: HTMLTextAreaElement) {
    setSearch(null);
    setTrigger(findTrigger(el.value, el.selectionStart, triggerChar));
  }

  /** Arrow keys move the caret without touching the text.
   *
   * An open popover keeps its token: the end follows the caret, so inserting
   * replaces exactly what the caret covers, and moving out of the token means
   * the DM is done with it. A sideways arrow that lands inside a tag opens it
   * again, which is the keyboard way back into a reference already written.
   *
   * Nothing else opens from here, and that is the point: the keyup of the very
   * key that inserted the token (Enter, or Tab) arrives here too, and re-reading
   * the token then would put the list straight back up on what was just
   * written. */
  function syncTokenEnd(caret: number, key: string) {
    setTrigger((prev) => {
      if (!prev) {
        if (key !== "ArrowLeft" && key !== "ArrowRight") return prev;
        const el = ref.current;
        return el ? findTrigger(el.value, caret, triggerChar) : prev;
      }
      const tokenEnd = prev.start + 1 + prev.query.length;
      if (caret < prev.start || caret > tokenEnd) return null;
      return caret === prev.end ? prev : { ...prev, end: caret };
    });
  }

  function select(page: PageLinkSuggestion) {
    if (!trigger) return;
    const slug = page.slug || slugify(page.title);
    const end = Math.max(trigger.start + 1, trigger.end);
    const next = value.slice(0, trigger.start) + triggerChar + slug + value.slice(end);
    onChange(next);
    close();
    // Put the caret right after the inserted token and keep focus.
    requestAnimationFrame(() => {
      const el = ref.current;
      if (!el) return;
      const pos = trigger.start + 1 + slug.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  }

  /** The tag, exactly as it will be written into the note.
   *
   * In the link colour, because a tag IS a reference. It also goes to the page
   * when the caller told us the campaign - the DM is choosing which page a name
   * means, and looking at one is part of choosing. Another tab on purpose: they
   * are mid-note, and following a tag must not cost them the draft. */
  function renderTag(page: PageLinkSuggestion) {
    const label = triggerChar + (page.slug || slugify(page.title));
    const className =
      "block truncate text-[11px] font-medium text-[color:var(--rl-accent-500)]";
    if (!campaignId) {
      return <span className={className}>{label}</span>;
    }
    return (
      <Link
        href={"/campaigns/" + campaignId + "/pages/" + page.id}
        target="_blank"
        rel="noreferrer"
        title={"Open " + page.title + " in a new tab"}
        onClick={(e) => e.stopPropagation()}
        className={className + " hover:underline"}
      >
        {label}
      </Link>
    );
  }

  function move(delta: number) {
    if (shown.length === 0) return;
    setHighlight((h) => (h + delta + shown.length) % shown.length);
  }

  function takeHighlighted() {
    const page = shown[highlight];
    if (page) select(page);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (!trigger) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (shown.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      takeHighlighted();
    }
  }

  function onSearchKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      ref.current?.focus();
      return;
    }
    if (shown.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      takeHighlighted();
    }
  }

  return (
    <div
      ref={wrapRef}
      className={"relative " + className}
      // Focus leaving the editor for anything outside it closes the popover.
      // React blurs bubble, so this covers the textarea and the search field
      // alike; moving between the two is not leaving.
      onBlur={(e) => {
        const next = e.relatedTarget;
        if (next instanceof Node && wrapRef.current?.contains(next)) return;
        close();
      }}
    >
      <textarea
        ref={ref}
        rows={rows}
        value={value}
        placeholder={placeholder}
        onChange={(e) => handleChange(e.target.value, e.target.selectionStart)}
        onClick={(e) => syncFromCaret(e.currentTarget)}
        onKeyUp={(e) => syncTokenEnd(e.currentTarget.selectionStart, e.key)}
        onKeyDown={onKeyDown}
        className="w-full rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] px-3 py-2 text-sm text-[color:var(--rl-text-on-parchment-primary)] placeholder:text-[color:var(--rl-text-on-parchment-muted)] outline-none focus:border-[color:var(--rl-accent-500)]"
      />
      {trigger ? (
        <div
          ref={panelRef}
          style={{
            left: anchor?.left ?? 0,
            top: anchor?.top ?? 0,
            width: anchor?.width ?? PANEL_MAX_W,
            // The first commit happens before place() has measured anything;
            // the layout effect corrects it before the browser paints, so this
            // only guards against a popover flashing at the corner.
            visibility: anchor ? "visible" : "hidden",
          }}
          // A click on the panel's own chrome should not steal focus from the
          // textarea; the search field is typed into, and a tag is a link that
          // has to keep its own behaviour.
          onMouseDown={(e) => {
            if (!(e.target as HTMLElement).closest("input, a")) e.preventDefault();
          }}
          className="absolute z-30 overflow-hidden rounded-[var(--rl-radius-md)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] shadow-[var(--rl-shadow-raised)]"
        >
          <div className="border-b border-[color:var(--rl-border-parchment)] p-2">
            <span className="relative block">
              <RlIcon
                name="search"
                size={15}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[color:var(--rl-text-on-parchment-muted)]"
              />
              <input
                type="text"
                value={query}
                role="combobox"
                aria-label="Search wiki pages"
                aria-autocomplete="list"
                aria-expanded={shown.length > 0}
                aria-controls={listId}
                aria-activedescendant={shown.length > 0 ? listId + "-" + highlight : undefined}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={onSearchKeyDown}
                placeholder="Search pages…"
                className="w-full rounded-[var(--rl-radius-sm)] border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-parchment-sunk)] py-1.5 pl-8 pr-2 text-sm text-[color:var(--rl-text-on-parchment-primary)] placeholder:text-[color:var(--rl-text-on-parchment-muted)] outline-none focus:border-[color:var(--rl-accent-500)]"
              />
            </span>
          </div>

          <div
            ref={listRef}
            id={listId}
            role="listbox"
            aria-label="Wiki pages"
            className="relative overflow-y-auto py-1"
            style={{ maxHeight: anchor?.listMaxHeight ?? PANEL_LIST_MAX_H }}
          >
            {shown.length === 0 ? (
              <p className="px-3 py-4 text-center text-xs text-[color:var(--rl-text-on-parchment-muted)]">
                {pages && pages.length > 0
                  ? "No page matches \u201C" + query.trim() + "\u201D."
                  : "This campaign has no wiki pages yet."}
              </p>
            ) : (
              shown.map((page, i) => (
                // A div rather than a button, because the tag inside it is a
                // LINK and interactive content may not live inside a button.
                // Focus never enters the list anyway: it stays in the textarea
                // (or the search field) and the active row is named by
                // aria-activedescendant, which is the listbox pattern this is.
                <div
                  key={page.id}
                  ref={(el) => {
                    rowRefs.current[i] = el;
                  }}
                  id={listId + "-" + i}
                  role="option"
                  aria-selected={i === highlight}
                  onMouseEnter={() => setHighlight(i)}
                  onClick={() => select(page)}
                  className={
                    "flex w-full cursor-pointer items-center gap-3 px-3 py-1.5 text-left transition " +
                    (i === highlight
                      ? "rl-bg-accent-soft rl-text-accent"
                      : "text-[color:var(--rl-text-on-parchment-primary)] hover:bg-[color:var(--rl-bg-parchment-sunk)]")
                  }
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{markMatch(page.title, query)}</span>
                    {renderTag(page)}
                  </span>
                  <span className="shrink-0 text-[10px] uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">
                    {kindLabel(page.kind)}
                  </span>
                </div>
              ))
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-[color:var(--rl-border-parchment)] px-3 py-1.5 text-[10px] uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">
            <span>
              {matches.length === 0
                ? "No match"
                : matches.length > shown.length
                  ? shown.length + " of " + matches.length + (query.trim() ? " matches" : " pages")
                  : matches.length +
                    (query.trim() ? (matches.length === 1 ? " match" : " matches") : " pages")}
            </span>
            <span>{shown.length > 0 ? "\u2191\u2193 move \u00B7 \u21B5 insert" : ""}</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

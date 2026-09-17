// Ravenlore design-system kit.
//
// The chrome rules of .agents/skills/ravenlore-frontend-design expressed as
// React primitives: one "fire" accent, warm-black dark chrome, parchment
// content surfaces, serif for titles only. The tokens and the .rl-* classes
// live in app/globals.css — this file never hard-codes a colour.
//
// The older slate primitives in components/ui.tsx are the legacy set that the
// not-yet-revamped screens still use; new work should reach for these instead.

"use client";

import Link from "next/link";
import type { ButtonHTMLAttributes, CSSProperties, InputHTMLAttributes, ReactNode } from "react";

// -------------------------------------------------------------------- icons

export type RlIconName =
  | "book"
  | "flame"
  | "crown"
  | "mic"
  | "search"
  | "chevron"
  | "plus"
  | "user"
  | "key"
  | "arrow"
  | "spark"
  | "gear"
  | "logout"
  | "download"
  | "home"
  | "clock"
  | "users"
  | "pin"
  | "flag"
  | "gem"
  | "scroll"
  | "image"
  | "play"
  | "pause"
  | "volume"
  | "mute"
  | "back10"
  | "forward10";

const ICON_PATHS: Record<RlIconName, ReactNode> = {
  book: (
    <>
      <path d="M2 4.5A1.5 1.5 0 0 1 3.5 3H9a3 3 0 0 1 3 3v13a3 3 0 0 0-3-3H2z" />
      <path d="M22 4.5A1.5 1.5 0 0 0 20.5 3H15a3 3 0 0 0-3 3v13a3 3 0 0 1 3-3h7z" />
    </>
  ),
  flame: (
    <path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z" />
  ),
  crown: <path d="M3 18.5h18M4.5 8.5l3.8 3.6L12 5.5l3.7 6.6 3.8-3.6-1.4 8H5.9z" />,
  mic: (
    <>
      <path d="M12 2.5a2.8 2.8 0 0 0-2.8 2.8v6a2.8 2.8 0 0 0 5.6 0v-6A2.8 2.8 0 0 0 12 2.5z" />
      <path d="M18.8 11v1.2a6.8 6.8 0 0 1-13.6 0V11M12 19v2.5" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-3.7-3.7" />
    </>
  ),
  chevron: <path d="m9.5 6 6 6-6 6" />,
  plus: <path d="M12 5.5v13M5.5 12h13" />,
  user: (
    <>
      <circle cx="12" cy="8" r="3.8" />
      <path d="M4.8 20.5a7.2 7.2 0 0 1 14.4 0" />
    </>
  ),
  key: (
    <>
      <circle cx="7.8" cy="15.8" r="3.8" />
      <path d="m10.6 13 8.4-8.4 2.2 2.2-1.8 1.8 1.5 1.5" />
    </>
  ),
  arrow: <path d="M4 12h15m-6-6.5 6.5 6.5-6.5 6.5" />,
  spark: <path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z" />,
  gear: (
    <>
      <circle cx="12" cy="12" r="3.1" />
      <path d="M19.1 14.4a1.6 1.6 0 0 0 .32 1.77l.05.06a1.9 1.9 0 1 1-2.69 2.69l-.06-.06a1.6 1.6 0 0 0-1.77-.32 1.6 1.6 0 0 0-.97 1.47v.16a1.9 1.9 0 1 1-3.8 0v-.09a1.6 1.6 0 0 0-1.04-1.47 1.6 1.6 0 0 0-1.77.32l-.06.06a1.9 1.9 0 1 1-2.69-2.69l.06-.06a1.6 1.6 0 0 0 .32-1.77 1.6 1.6 0 0 0-1.47-.97H3.3a1.9 1.9 0 1 1 0-3.8h.09a1.6 1.6 0 0 0 1.47-1.04 1.6 1.6 0 0 0-.32-1.77l-.06-.06a1.9 1.9 0 1 1 2.69-2.69l.06.06a1.6 1.6 0 0 0 1.77.32h.08A1.6 1.6 0 0 0 10.2 3.3v-.1a1.9 1.9 0 1 1 3.8 0v.09a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.77-.32l.06-.06a1.9 1.9 0 1 1 2.69 2.69l-.06.06a1.6 1.6 0 0 0-.32 1.77v.08a1.6 1.6 0 0 0 1.47.97h.16a1.9 1.9 0 1 1 0 3.8h-.09a1.6 1.6 0 0 0-1.54.97z" />
    </>
  ),
  logout: (
    <>
      <path d="M9.5 20.5H5.5a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h4" />
      <path d="m16 16.5 4.5-4.5L16 7.5" />
      <path d="M20.5 12H9.5" />
    </>
  ),
  download: (
    <>
      <path d="M12 3.5v11" />
      <path d="m7.5 10 4.5 4.5 4.5-4.5" />
      <path d="M4.5 20.5h15" />
    </>
  ),
  home: (
    <>
      <path d="M3.5 10.6 12 3.5l8.5 7.1" />
      <path d="M5.6 9.4V20.5h12.8V9.4" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.2" />
      <path d="M12 7.5V12l3 1.8" />
    </>
  ),
  users: (
    <>
      <circle cx="9.5" cy="8.5" r="3.3" />
      <path d="M3.5 20a6 6 0 0 1 12 0" />
      <path d="M16 5.6a3.3 3.3 0 0 1 0 6.4M17.5 20a6 6 0 0 0-2-4.5" />
    </>
  ),
  pin: (
    <>
      <path d="M12 20.5s6.5-6 6.5-10.4a6.5 6.5 0 1 0-13 0C5.5 14.5 12 20.5 12 20.5z" />
      <circle cx="12" cy="10" r="2.4" />
    </>
  ),
  flag: (
    <>
      <path d="M6.2 20.5V4" />
      <path d="M6.2 4.8h11.3l-1.6 3.6 1.6 3.6H6.2" />
    </>
  ),
  gem: (
    <>
      <path d="m12 3.5 7.5 6-7.5 11-7.5-11z" />
      <path d="M4.5 9.5h15M12 3.5 9.4 9.5 12 20.5l2.6-11z" />
    </>
  ),
  scroll: (
    <>
      <path d="M7 20.5h10.5a2 2 0 0 0 2-2V4.5" />
      <path d="M7 20.5a2 2 0 0 1-2-2v-12a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v3H7" />
      <path d="M8.5 9.5h6M8.5 13h6" />
    </>
  ),
  image: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <circle cx="9" cy="10" r="1.7" />
      <path d="m4.5 17.5 4.4-4.4 3.6 3.6 3.1-3.1 4 4" />
    </>
  ),
  play: <path d="M8.6 5.4 18.8 12 8.6 18.6z" fill="currentColor" />,
  pause: (
    <>
      <rect x="7.6" y="5.5" width="3.4" height="13" rx="1.1" fill="currentColor" stroke="none" />
      <rect x="13" y="5.5" width="3.4" height="13" rx="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  volume: (
    <>
      <path d="M4.5 9.5h3l4-3.5v12l-4-3.5h-3z" />
      <path d="M15.4 9.8a3.2 3.2 0 0 1 0 4.4M17.9 7.4a6.6 6.6 0 0 1 0 9.2" />
    </>
  ),
  mute: (
    <>
      <path d="M4.5 9.5h3l4-3.5v12l-4-3.5h-3z" />
      <path d="m15.8 10 4 4M19.8 10l-4 4" />
    </>
  ),
  back10: (
    <>
      <path d="M11.8 4.6a7.4 7.4 0 1 1-7.1 5.3" />
      <path d="M11.9 1.9 9.1 4.6l2.8 2.7" />
      <text
        x="12"
        y="15.4"
        textAnchor="middle"
        fontSize="7"
        fontWeight="700"
        fill="currentColor"
        stroke="none"
      >
        10
      </text>
    </>
  ),
  forward10: (
    <>
      <path d="M12.2 4.6a7.4 7.4 0 1 0 7.1 5.3" />
      <path d="M12.1 1.9l2.8 2.7-2.8 2.7" />
      <text
        x="12"
        y="15.4"
        textAnchor="middle"
        fontSize="7"
        fontWeight="700"
        fill="currentColor"
        stroke="none"
      >
        10
      </text>
    </>
  ),
};

export function RlIcon({
  name,
  size = 18,
  className = "",
}: {
  name: RlIconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {ICON_PATHS[name]}
    </svg>
  );
}

// -------------------------------------------------------------------- tints

/** Category/status tints. "accent" and "muted" are the non-category pair. */
export type RlTint =
  | "villain"
  | "place"
  | "npc"
  | "item"
  | "faction"
  | "neutral"
  | "ink"
  | "plum"
  | "accent"
  | "muted";

const TINT_VAR: Record<RlTint, string> = {
  villain: "var(--rl-cat-villain)",
  place: "var(--rl-cat-place)",
  npc: "var(--rl-cat-npc)",
  item: "var(--rl-cat-item)",
  faction: "var(--rl-cat-faction)",
  neutral: "var(--rl-cat-neutral)",
  ink: "var(--rl-cat-ink)",
  plum: "var(--rl-cat-plum)",
  accent: "var(--rl-accent-500)",
  muted: "var(--rl-text-on-parchment-muted)",
};

/** Sets --tint for the .rl-tag / .rl-icon-chip / .rl-tint classes. */
export function rlTintVars(tint: RlTint): CSSProperties {
  return { "--tint": TINT_VAR[tint] } as CSSProperties;
}

// Amber = the pipeline is working, violet = it is waiting for the DM,
// green = settled, rust = failed. Mirrors SESSION_STATUS_TONE in ui.tsx.
const SESSION_STATUS_TINT: Record<string, RlTint> = {
  uploaded: "muted",
  recorded: "place",
  transcribing: "item",
  transcribed: "item",
  refining: "item",
  refined: "item",
  identifying_speakers: "npc",
  speakers_identified: "npc",
  speaker_pending: "npc",
  attributing: "npc",
  attribution_ready: "npc",
  attribution_review: "npc",
  summarizing: "item",
  summary_ready: "npc",
  generating_wiki: "npc",
  wiki_plan_ready: "npc",
  applying_wiki: "item",
  content_ready: "neutral",
  reviewed: "neutral",
  published: "neutral",
  failed: "villain",
};

export function sessionStatusTint(status: string): RlTint {
  return SESSION_STATUS_TINT[status] ?? "muted";
}

// ------------------------------------------------------------------ buttons

export type RlButtonVariant = "primary" | "outline" | "on-dark" | "ghost";

const BUTTON_VARIANT_CLASS: Record<RlButtonVariant, string> = {
  primary: "rl-btn--primary",
  outline: "rl-btn--outline",
  "on-dark": "rl-btn--on-dark",
  ghost: "rl-btn--ghost",
};

function classes(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function rlButtonClass(variant: RlButtonVariant = "primary", ...rest: (string | false)[]): string {
  return classes("rl-btn", BUTTON_VARIANT_CLASS[variant], ...rest);
}

export function RlButton({
  variant = "primary",
  size = "md",
  block = false,
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: RlButtonVariant;
  size?: "sm" | "md";
  block?: boolean;
}) {
  return (
    <button className={rlButtonClass(variant, size === "sm" && "rl-btn--sm", block && "rl-btn--block", className)} {...props}>
      {children}
    </button>
  );
}

/** Navigation dressed as a button — a link, not an action. */
export function RlButtonLink({
  href,
  variant = "primary",
  size = "md",
  className = "",
  children,
}: {
  href: string;
  variant?: RlButtonVariant;
  size?: "sm" | "md";
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={rlButtonClass(variant, size === "sm" && "rl-btn--sm", className)}>
      {children}
    </Link>
  );
}

// --------------------------------------------------------------------- tags

export function RlTag({
  tint = "muted",
  onDark = false,
  children,
}: {
  tint?: RlTint;
  onDark?: boolean;
  children: ReactNode;
}) {
  return (
    <span className={classes("rl-tag", onDark && "rl-tag--on-dark")} style={rlTintVars(tint)}>
      {children}
    </span>
  );
}

/** Tinted square icon chip (card sigil, stat tile, list row). */
export function RlIconChip({
  name,
  tint = "neutral",
  size = 36,
  iconSize = 18,
}: {
  name: RlIconName;
  tint?: RlTint;
  size?: number;
  iconSize?: number;
}) {
  return (
    <span className="rl-icon-chip" style={{ width: size, height: size, ...rlTintVars(tint) }}>
      <RlIcon name={name} size={iconSize} />
    </span>
  );
}

// -------------------------------------------------------------------- cards

export function RlCard({ href, children, className = "" }: { href?: string; children: ReactNode; className?: string }) {
  const cls = classes("rl-card", className);
  if (href) {
    return (
      <Link href={href} className={cls}>
        {children}
      </Link>
    );
  }
  return <div className={cls}>{children}</div>;
}

// --------------------------------------------------------------- stat tiles

export function RlStatRow({ children }: { children: ReactNode }) {
  return <div className="rl-stat-row">{children}</div>;
}

export function RlStat({
  icon,
  tint = "neutral",
  value,
  label,
}: {
  icon: RlIconName;
  tint?: RlTint;
  value: ReactNode;
  label: string;
}) {
  return (
    <div className="rl-stat">
      <RlIconChip name={icon} tint={tint} />
      <span className="min-w-0">
        <span className="rl-stat-value block truncate">{value}</span>
        <span className="rl-stat-label block truncate">{label}</span>
      </span>
    </div>
  );
}

// ------------------------------------------------------------ panels & rows

export function RlPanel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={classes("rl-panel", className)}>{children}</section>;
}

export function RlPanelHead({
  eyebrow,
  meta,
  action,
}: {
  eyebrow: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="rl-panel-head flex flex-wrap items-center justify-between gap-2">
      <span className="rl-eyebrow">{eyebrow}</span>
      {action ?? (meta ? <span className="rl-card-meta">{meta}</span> : null)}
    </header>
  );
}

/** Section kicker + optional serif title, with room for a count or an action. */
export function RlSectionHead({
  eyebrow,
  title,
  meta,
  action,
}: {
  eyebrow: ReactNode;
  title?: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <span className="rl-eyebrow">{eyebrow}</span>
        {title ? <h2 className="rl-title mt-1 text-[22px]">{title}</h2> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {meta ? <span className="rl-card-meta">{meta}</span> : null}
        {action}
      </div>
    </div>
  );
}

export function RlListRow({
  href,
  icon,
  tint = "neutral",
  title,
  sub,
  badge,
  trailing,
}: {
  href: string;
  icon: RlIconName;
  tint?: RlTint;
  title: ReactNode;
  sub: ReactNode;
  /** A pill (tag, status badge) shown before the trailing meta. */
  badge?: ReactNode;
  trailing?: ReactNode;
}) {
  return (
    <Link href={href} className="rl-list-row">
      <RlIconChip name={icon} tint={tint} size={34} iconSize={17} />
      <span className="min-w-0 flex-1">
        <span className="rl-list-row-title block truncate">{title}</span>
        <span className="rl-list-row-sub block truncate">{sub}</span>
      </span>
      {badge ? <span className="shrink-0">{badge}</span> : null}
      {trailing ? <span className="rl-list-row-sub shrink-0 tabular-nums">{trailing}</span> : null}
      <RlIcon name="chevron" size={16} className="rl-chevron" />
    </Link>
  );
}

// --------------------------------------------------------------- dark panel

export function RlDarkPanel({
  title,
  icon,
  note,
  children,
}: {
  title: ReactNode;
  icon?: RlIconName;
  note?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rl-dark-panel">
      <header className="flex items-center gap-2.5">
        {icon ? <RlIconChip name={icon} tint="accent" size={30} iconSize={16} /> : null}
        <h2 className="rl-dark-title">{title}</h2>
      </header>
      {note ? <p className="rl-dark-note mt-3">{note}</p> : null}
      <div className="mt-4">{children}</div>
    </section>
  );
}

// ------------------------------------------------------------------- inputs

export function RlField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="rl-field-label">{label}</span>
      {children}
      {hint ? <span className="mt-1.5 block text-xs text-[color:var(--rl-text-on-dark-muted)]">{hint}</span> : null}
    </label>
  );
}

export function RlTextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="rl-input" {...props} />;
}

export function RlSearchInput({
  value,
  onChange,
  placeholder = "Search…",
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel?: string;
}) {
  return (
    <div className="rl-search">
      <RlIcon name="search" size={16} className="rl-search-icon" />
      <input
        type="search"
        value={value}
        aria-label={ariaLabel ?? placeholder}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export function RlChip({
  active = false,
  onClick,
  children,
}: {
  active?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} aria-pressed={active} className={classes("rl-chip", active && "rl-chip--active")}>
      {children}
    </button>
  );
}

// ------------------------------------------------------- feedback & states

export function RlAlert({
  tone = "error",
  onDark = false,
  children,
}: {
  tone?: "error" | "success";
  onDark?: boolean;
  children: ReactNode;
}) {
  const variant = onDark && tone === "error" ? "rl-alert--error-on-dark" : classes("rl-alert--" + tone);
  return (
    <div role={tone === "error" ? "alert" : "status"} className={classes("rl-alert", variant)}>
      {children}
    </div>
  );
}

export function RlEmptyState({
  icon,
  tint = "muted",
  title,
  children,
  action,
}: {
  icon?: RlIconName;
  tint?: RlTint;
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="rl-empty">
      {icon ? (
        <div className="mb-4 flex justify-center">
          <RlIconChip name={icon} tint={tint} size={44} iconSize={22} />
        </div>
      ) : null}
      <p className="rl-empty-title">{title}</p>
      {children ? <div className="rl-body mx-auto mt-2 max-w-[52ch]">{children}</div> : null}
      {action ? <div className="mt-5 flex justify-center">{action}</div> : null}
    </div>
  );
}

// -------------------------------------------------------------------- utils

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000000],
  ["month", 2592000000],
  ["week", 604800000],
  ["day", 86400000],
  ["hour", 3600000],
  ["minute", 60000],
];

/** "3 days ago" / "in 2 hours" — for meta lines, where an exact stamp is noise. */
export function fmtRelative(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const diff = date.getTime() - Date.now();
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, ms] of RELATIVE_UNITS) {
    if (abs >= ms) return rtf.format(Math.round(diff / ms), unit);
  }
  return "just now";
}

/** Millisecond sort key for a session: when it was played, else when it was uploaded. */
export function sessionStamp(session: { recorded_at: string | null; created_at: string }): number {
  const played = session.recorded_at ? Date.parse(session.recorded_at) : NaN;
  if (!Number.isNaN(played)) return played;
  const created = Date.parse(session.created_at);
  return Number.isNaN(created) ? 0 : created;
}

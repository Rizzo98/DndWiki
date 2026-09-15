// Small shared UI primitives (dark slate theme, Tailwind).

import Link from "next/link";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";

// ---------------------------------------------------------------- buttons

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

const BUTTON_STYLES: Record<ButtonVariant, string> = {
  primary: "bg-ember-500 text-slate-950 hover:bg-ember-400 disabled:opacity-50",
  secondary: "border border-slate-700 bg-slate-800 text-slate-100 hover:bg-slate-700 disabled:opacity-50",
  danger: "bg-red-600/90 text-white hover:bg-red-500 disabled:opacity-50",
  ghost: "text-slate-300 hover:bg-slate-800 disabled:opacity-50",
};

/** Button styling shared by <Button> and <ButtonLink>. */
export function buttonClass(variant: ButtonVariant = "primary", className = ""): string {
  return `inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed ${BUTTON_STYLES[variant]} ${className}`;
}

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return <button className={buttonClass(variant, className)} {...props} />;
}

/** Navigation dressed as a button (a link, not an action). */
export function ButtonLink({
  href,
  variant = "primary",
  className = "",
  children,
}: {
  href: string;
  variant?: ButtonVariant;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={buttonClass(variant, className)}>
      {children}
    </Link>
  );
}

// ------------------------------------------------------------------ forms

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-400">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-xs text-slate-500">{hint}</span> : null}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-ember-500"
      {...props}
    />
  );
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-ember-500"
      {...props}
    />
  );
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-ember-500"
      {...props}
    />
  );
}

export function FileInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      type="file"
      className="block w-full text-sm text-slate-300 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-700 file:px-3 file:py-2 file:text-sm file:font-semibold file:text-slate-100 hover:file:bg-slate-600"
      {...props}
    />
  );
}

// ------------------------------------------------------------------- card

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900 p-5 ${className}`}>{children}</div>
  );
}

// ------------------------------------------------------------------ badge

type BadgeTone = "slate" | "blue" | "amber" | "emerald" | "red" | "violet" | "orange" | "green";

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: BadgeTone }) {
  const tones: Record<BadgeTone, string> = {
    slate: "bg-slate-700/60 text-slate-200",
    blue: "bg-blue-500/15 text-blue-300",
    amber: "bg-amber-500/15 text-amber-300",
    emerald: "bg-emerald-500/15 text-emerald-300",
    green: "bg-green-500/15 text-green-300",
    red: "bg-red-500/15 text-red-300",
    violet: "bg-violet-500/15 text-violet-300",
    orange: "bg-orange-500/15 text-orange-300",
  };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

// --------------------------------------------------------------- statuses

export const SESSION_STATUS_TONE: Record<string, BadgeTone> = {
  uploaded: "slate",
  recorded: "blue",
  transcribing: "amber",
  transcribed: "green",
  refining: "amber",
  refined: "blue",
  identifying_speakers: "violet",
  speakers_identified: "violet",
  // retired as a blocking state; shown only for in-flight sessions
  speaker_pending: "orange",
  attributing: "violet",
  attribution_ready: "violet",
  // the resting review state: the pipeline is waiting for the DM, not working
  attribution_review: "blue",
  summarizing: "amber",
  summary_ready: "violet",
  generating_wiki: "violet",
  wiki_plan_ready: "violet",
  applying_wiki: "amber",
  content_ready: "emerald",
  reviewed: "green",
  published: "green",
  failed: "red",
};

export function SessionStatusBadge({ status }: { status: string }) {
  return <Badge tone={SESSION_STATUS_TONE[status] ?? "slate"}>{status.replace(/_/g, " ")}</Badge>;
}

// A page is either a draft, published or archived — there is no 'pending
// review' state (the pipeline only writes confirmed changes).
export const PAGE_STATUS_TONE: Record<string, BadgeTone> = {
  draft: "slate",
  published: "green",
  archived: "slate",
};

export function PageStatusBadge({ status }: { status: string }) {
  return <Badge tone={PAGE_STATUS_TONE[status] ?? "slate"}>{status.replace(/_/g, " ")}</Badge>;
}

export const VISIBILITY_TONE: Record<string, BadgeTone> = {
  public: "green",
  dm_only: "amber",
  hidden: "red",
};

export function VisibilityBadge({ visibility }: { visibility: string }) {
  return <Badge tone={VISIBILITY_TONE[visibility] ?? "slate"}>{visibility.replace(/_/g, " ")}</Badge>;
}

// -------------------------------------------------------------- feedback

export function Alert({ tone = "error", children }: { tone?: "error" | "success" | "info"; children: ReactNode }) {
  const tones = {
    error: "border-red-800 bg-red-950/50 text-red-200",
    success: "border-emerald-800 bg-emerald-950/50 text-emerald-200",
    info: "border-slate-700 bg-slate-800/60 text-slate-300",
  };
  return <div className={`rounded-lg border px-4 py-3 text-sm ${tones[tone]}`}>{children}</div>;
}

/**
 * A collapsed disclosure, closed by default.
 *
 * The redesign's main UX change is that the plumbing moves DOWN the page: the
 * raw transcript and the speaker/voice machinery are things the DM reaches for
 * when they want them, not what the page leads with (docs/attribution-ux.md S2,
 * S7). <details> is used rather than a JS disclosure so the content is still in
 * the DOM (searchable, and reachable without JavaScript).
 */
export function Collapsible({
  summary,
  children,
  defaultOpen = false,
  tone = "slate",
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  tone?: "slate" | "subtle";
}) {
  return (
    <details
      open={defaultOpen}
      className={`group rounded-xl border p-5 ${
        tone === "subtle" ? "border-slate-800/60 bg-slate-900/40" : "border-slate-800 bg-slate-900"
      }`}
    >
      <summary className="cursor-pointer list-none text-sm font-semibold text-slate-200 marker:hidden">
        <span className="inline-flex items-center gap-2">
          <span className="text-slate-500 transition-transform group-open:rotate-90">▶</span>
          {summary}
        </span>
      </summary>
      <div className="mt-4">{children}</div>
    </details>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-slate-800 px-4 py-10 text-center text-sm text-slate-500">{children}</div>;
}

export function Spinner() {
  return <div className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-ember-500" />;
}

// ----------------------------------------------------------------- utils

export function fmtDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

export function fmtDuration(sec?: number | null): string {
  if (sec === null || sec === undefined) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

export function fmtPercent(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}
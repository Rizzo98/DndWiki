// Small shared UI primitives — the legacy call-signs the older screens import.
//
// The chrome they render now comes from the Ravenlore design system
// (components/ravenlore.tsx plus the .rl-* classes in app/globals.css), so a
// screen written before the redesign still looks like the same product. New
// work should import components/ravenlore directly.

import Link from "next/link";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { RlTag, sessionStatusTint, type RlTint } from "./ravenlore";

// ---------------------------------------------------------------- buttons

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

const BUTTON_VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "rl-btn--primary",
  secondary: "rl-btn--outline",
  danger: "rl-btn--danger",
  ghost: "rl-btn--ghost",
};

/** Button styling shared by <Button> and <ButtonLink>. */
export function buttonClass(variant: ButtonVariant = "primary", className = ""): string {
  return ("rl-btn " + BUTTON_VARIANT_CLASS[variant] + " " + className).trim();
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
      <span className="rl-eyebrow mb-1.5">{label}</span>
      {children}
      {hint ? <span className="rl-card-meta mt-1.5 block">{hint}</span> : null}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="rl-input-light" {...props} />;
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className="rl-input-light" {...props} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className="rl-input-light" {...props} />;
}

export function FileInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input type="file" className="rl-file" {...props} />;
}

// ------------------------------------------------------------------- card

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={"rl-card " + className}>{children}</div>;
}

// ------------------------------------------------------------------ badge

type BadgeTone = "slate" | "blue" | "amber" | "emerald" | "red" | "violet" | "orange" | "green";

/** Legacy badge tones mapped onto the design system's category tints. */
const BADGE_TINT: Record<BadgeTone, RlTint> = {
  slate: "muted",
  blue: "place",
  amber: "item",
  emerald: "neutral",
  green: "neutral",
  red: "villain",
  violet: "npc",
  orange: "item",
};

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: BadgeTone }) {
  return <RlTag tint={BADGE_TINT[tone] ?? "muted"}>{children}</RlTag>;
}

// --------------------------------------------------------------- statuses

export function SessionStatusBadge({ status }: { status: string }) {
  return <RlTag tint={sessionStatusTint(status)}>{status.replace(/_/g, " ")}</RlTag>;
}

// A page is either a draft, published or archived — there is no 'pending
// review' state (the pipeline only writes confirmed changes).
const PAGE_STATUS_TINT: Record<string, RlTint> = {
  draft: "muted",
  pending_review: "item",
  published: "neutral",
  archived: "muted",
};

export function PageStatusBadge({ status }: { status: string }) {
  return <RlTag tint={PAGE_STATUS_TINT[status] ?? "muted"}>{status.replace(/_/g, " ")}</RlTag>;
}

const VISIBILITY_TINT: Record<string, RlTint> = {
  public: "neutral",
  dm_only: "item",
  hidden: "villain",
};

export function VisibilityBadge({ visibility }: { visibility: string }) {
  return <RlTag tint={VISIBILITY_TINT[visibility] ?? "muted"}>{visibility.replace(/_/g, " ")}</RlTag>;
}

// -------------------------------------------------------------- feedback

export function Alert({ tone = "error", children }: { tone?: "error" | "success" | "info"; children: ReactNode }) {
  return (
    <div role={tone === "error" ? "alert" : "status"} className={"rl-alert rl-alert--" + tone}>
      {children}
    </div>
  );
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
      className={"group rl-panel" + (tone === "subtle" ? " border-dashed opacity-95" : "")}
    >
      <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold marker:hidden">
        <span className="inline-flex items-center gap-2">
          <span className="text-[color:var(--rl-text-on-parchment-muted)] transition-transform group-open:rotate-90">
            ▶
          </span>
          {summary}
        </span>
      </summary>
      <div className="border-t border-[color:var(--rl-border-parchment)] px-5 py-4">{children}</div>
    </details>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="rl-empty text-sm">{children}</div>;
}

export function Spinner() {
  return (
    <div className="h-4 w-4 animate-spin rounded-full border-2 border-[color:var(--rl-border-parchment-strong)] border-t-[color:var(--rl-accent-500)]" />
  );
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
  return m > 0 ? m + "m " + s + "s" : s + "s";
}

export function fmtPercent(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return Math.round(value * 100) + "%";
}

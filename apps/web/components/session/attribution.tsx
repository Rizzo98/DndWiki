// Attribution UI primitives: how the page tells the DM how sure the engine is.
//
// The whole point of the redesign is that uncertainty is FIRST-CLASS rather than
// destroyed at a service boundary, so it has to be visible without being
// alarming (docs/attribution-ux.md S5):
//
//   user_confirmed  solid name, green   - the DM said so
//   auto_high       solid name, neutral - observed, corroborated
//   propagated      name + link icon    - inferred from an answer
//   auto_low        name + "?", amber   - plausible, not enough to write a fact
//   unresolved      "someone", grey     - the engine does not know

"use client";

import { Badge, fmtPercent } from "@/components/ui";
import type { AttributionStatus } from "@/lib/api";

type Tone = "slate" | "blue" | "amber" | "emerald" | "red" | "violet" | "orange" | "green";

export const ATTRIBUTION_TONE: Record<AttributionStatus, Tone> = {
  user_confirmed: "emerald",
  auto_high: "blue",
  propagated: "violet",
  auto_low: "amber",
  unresolved: "slate",
};

export const ATTRIBUTION_LABEL: Record<AttributionStatus, string> = {
  user_confirmed: "you confirmed this",
  auto_high: "we're confident",
  propagated: "inferred from your answer",
  auto_low: "probably, not certain",
  unresolved: "we don't know who this was",
};

/** The statuses that may back a character-level fact on a wiki page. */
export const CONFIDENT: readonly AttributionStatus[] = [
  "user_confirmed",
  "auto_high",
  "propagated",
];

export function isConfident(status: AttributionStatus): boolean {
  return CONFIDENT.includes(status);
}

/**
 * The status of one utterance, as a chip.
 *
 * The title attribute carries the plain-language explanation: a DM should never
 * have to learn what "propagated" means to trust the page.
 */
export function ProvenanceChip({
  status,
  confidence,
  decidedBy,
}: {
  status: AttributionStatus;
  confidence?: number | null;
  decidedBy?: string | null;
}) {
  const title = [
    ATTRIBUTION_LABEL[status],
    confidence != null ? `confidence ${fmtPercent(confidence)}` : null,
    decidedBy ? `decided by ${decidedBy}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <span title={title}>
      <Badge tone={ATTRIBUTION_TONE[status]}>{status.replace(/_/g, " ")}</Badge>
    </span>
  );
}

/**
 * How much of the session is actually understood, and how far along the review is.
 *
 * Stakes-weighted SPEECH SECONDS, not "3 of 5 labels": the old figure said
 * nothing about how much of a four-hour session was covered, and it was the one
 * the DM was shown (docs/attribution-model.md S11.1).
 *
 * Two things this must never do, because it did both:
 *
 * - claim a NUMBER OF QUESTIONS TO FINISH it cannot support. The greedy
 *   simulation stops at MAX_QUESTIONS, so a plan that ends there is a LOWER
 *   BOUND ("8 or more"), not a prediction; "about 8 questions to finish" was
 *   followed by eight answers and 82% of what matters still unattributed.
 * - present the coverage as a bar that only fills up. It measures what the wiki
 *   will ACT on, and the honest framing is what is left, not a promise of 100%.
 */
export function CoverageBar({
  coverage,
  unresolved,
  plan,
  finished,
}: {
  coverage: number;
  unresolved?: number;
  plan?: { questions: number | null; answered: number; max_questions: number; is_lower_bound: boolean };
  finished?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(coverage * 100)));
  const answered = plan?.answered ?? 0;
  const planned = plan?.questions ?? null;
  const remaining = planned != null ? Math.max(0, planned - answered) : null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-sm">
        <span className="font-semibold text-slate-200">We identified {pct}% of your session</span>
        {finished ? (
          <span className="text-slate-400">
            review finished{answered > 0 ? ` · ${answered} answered` : ""}
          </span>
        ) : plan == null ? null : plan.is_lower_bound ? (
          <span className="text-slate-400">
            {answered} of {plan.max_questions} answered · this session needs more questions
            than we ask in one sitting
          </span>
        ) : remaining != null && remaining > 0 ? (
          <span className="text-slate-400">
            {answered > 0 ? `${answered} answered · ` : ""}about {remaining} question
            {remaining === 1 ? "" : "s"} to finish
          </span>
        ) : (
          <span className="text-slate-400">{answered} of {planned} answered</span>
        )}
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-slate-800"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="h-full rounded-full bg-ember-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
      {unresolved != null && unresolved > 0 ? (
        <p className="text-xs text-slate-500">
          {fmtPercent(unresolved)} of what matters is still unattributed
          {finished ? "" : " so far"}. Anything left unresolved is described at party level and
          never attributed to a character.
        </p>
      ) : null}
    </div>
  );
}

/** The buckets, so the DM can see where the uncertainty actually sits. */
export function AttributionBuckets({
  buckets,
}: {
  buckets: Partial<Record<AttributionStatus, number>>;
}) {
  const order: AttributionStatus[] = ["user_confirmed", "auto_high", "propagated", "auto_low", "unresolved"];
  const present = order.filter((status) => (buckets[status] ?? 0) > 0);
  if (present.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {present.map((status) => (
        <span key={status} className="inline-flex items-center gap-1.5 text-xs text-slate-400">
          <Badge tone={ATTRIBUTION_TONE[status]}>{buckets[status]}</Badge>
          {status.replace(/_/g, " ")}
        </span>
      ))}
    </div>
  );
}

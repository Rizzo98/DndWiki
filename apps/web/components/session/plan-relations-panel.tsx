// PlanRelationsPanel — the "Links between pages" half of a change set.
//
// A relation connects two page titles. Whether it is written depends on
// whether BOTH ends exist once the change set lands: the wiki-service resolves
// each title against the pages it just created plus the pages the campaign
// already had, and silently skips a link it cannot resolve. The review says so
// up front, per row, so a dangling proposition is visible before confirming
// instead of missing afterwards.

"use client";

import { RlButton, RlIcon, RlPanel, RlPanelHead, RlTag, rlTintVars, type RlTint } from "@/components/ravenlore";
import { PAGE_KIND_ICON, PAGE_KIND_TINT } from "@/lib/page-kinds";
import type { PlanRelation, WikiPageKind } from "@/lib/api";

/** How one end of a relation reads inside the change set. */
export interface PlanEndpoint {
  title: string;
  /** existing: already a campaign page — proposed: created by this set —
   *  dropped: proposed here but the DM dropped it — missing: no page at all. */
  status: "existing" | "proposed" | "dropped" | "missing";
  kind: WikiPageKind | null;
}

/** Human wording of the relation types the pipeline proposes. */
const RELATION_LABELS: Record<string, string> = {
  appears_in: "appears in",
  member_of: "member of",
  allied_with: "allied with",
  led_by: "led by",
  owner: "owns",
  possible_duplicate: "possible duplicate of",
  located_in: "is in",
  part_of: "is part of",
};

function relationLabel(type: string): string {
  return RELATION_LABELS[type] ?? type.replace(/_/g, " ");
}

/** Whether this end of a link finds a page once the change set lands. */
export function endpointResolves(endpoint: PlanEndpoint): boolean {
  return endpoint.status === "existing" || endpoint.status === "proposed";
}

export function PlanRelationsPanel({
  relations,
  isDropped,
  resolve,
  canReview,
  applied,
  onToggleDropped,
}: {
  relations: PlanRelation[];
  isDropped: (relation: PlanRelation) => boolean;
  resolve: (title: string | null, pageId?: string | null) => PlanEndpoint;
  canReview: boolean;
  applied: boolean;
  onToggleDropped: (relation: PlanRelation, dropped: boolean) => void;
}) {
  const kept = relations.filter((relation) => {
    if (isDropped(relation)) return false;
    return (
      endpointResolves(resolve(relation.from_title)) &&
      endpointResolves(resolve(relation.to_title, relation.to_page_id))
    );
  }).length;

  return (
    <RlPanel>
      <RlPanelHead
        eyebrow="Links between pages"
        meta={
          relations.length === 0
            ? "none proposed"
            : kept + " of " + relations.length + " will be written"
        }
      />
      {relations.length === 0 ? (
        <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
          The session proposes no cross-references between pages.
        </p>
      ) : (
        <ul>
          {relations.map((relation) => {
            const from = resolve(relation.from_title);
            const to = resolve(relation.to_title, relation.to_page_id);
            const dropped = isDropped(relation);
            const broken = !endpointResolves(from)
              ? "no page to start from"
              : !endpointResolves(to)
                ? "no page to link to yet"
                : null;
            return (
              <li
                key={relation.id}
                className="flex flex-wrap items-center gap-x-2.5 gap-y-2 border-t border-[color:var(--rl-border-parchment)] px-4 py-3"
              >
                <Endpoint endpoint={from} strike={dropped} />
                <RlTag tint="accent">{relationLabel(relation.relation_type)}</RlTag>
                <RlIcon name="arrow" size={14} className="text-[color:var(--rl-text-on-parchment-muted)]" />
                <Endpoint endpoint={to} strike={dropped} />
                {dropped ? (
                  <RlTag tint="villain">dropped</RlTag>
                ) : broken ? (
                  <RlTag tint="villain">{broken}</RlTag>
                ) : null}
                {canReview && !applied ? (
                  <RlButton
                    variant="ghost"
                    size="sm"
                    className="ml-auto"
                    onClick={() => onToggleDropped(relation, !dropped)}
                  >
                    {dropped ? "Restore" : "Drop"}
                  </RlButton>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {relations.length > 0 ? (
        <p className="rl-card-meta border-t border-[color:var(--rl-border-parchment)] px-4 py-2.5">
          Written with the pages above. Both ends have to resolve to a page (by title or
          alias) once the set lands — a link that does not is skipped.
        </p>
      ) : null}
    </RlPanel>
  );
}

/** One end of a link: the page's sigil, its name, and how sure we are it exists. */
function Endpoint({ endpoint, strike }: { endpoint: PlanEndpoint; strike: boolean }) {
  const tint: RlTint = endpoint.kind ? PAGE_KIND_TINT[endpoint.kind] ?? "muted" : "muted";
  const icon = endpoint.kind ? PAGE_KIND_ICON[endpoint.kind] ?? "book" : "search";
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className="rl-tint shrink-0" style={rlTintVars(tint)}>
        <RlIcon name={icon} size={14} />
      </span>
      <span
        className={
          "truncate text-sm font-semibold " +
          (strike
            ? "text-[color:var(--rl-text-on-parchment-muted)] line-through"
            : "text-[color:var(--rl-text-on-parchment-primary)]")
        }
        title={endpoint.title}
      >
        {endpoint.title}
      </span>
      {endpoint.status === "proposed" ? <RlTag tint="neutral">new</RlTag> : null}
      {endpoint.status === "missing" ? <RlTag tint="villain">no page</RlTag> : null}
    </span>
  );
}

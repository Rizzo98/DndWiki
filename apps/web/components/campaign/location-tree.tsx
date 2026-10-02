// The Locations category, drawn as the tree the campaign actually is.
//
// A wiki page is stored flat: nothing on the page says that the "Locanda del
// Fumo Aspro" sits inside "Fatumastra", which sits inside "Luxastra". The
// containment is prose in attributes.region, and the API derives the nesting
// from it (services/wiki-service/app/services/locations.py). A campaign whose
// places name no container comes back as a flat list of roots, which this
// renders as the plain list it already was — just without guides or toggles.

"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { PageStatusBadge, VisibilityBadge } from "@/components/ui";
import {
  RlAlert,
  RlButton,
  RlEmptyState,
  RlIcon,
  RlIconChip,
  RlPanel,
  RlPanelHead,
  RlTag,
  fmtRelative,
  type RlTint,
} from "@/components/ravenlore";
import {
  LOCATION_TYPE_LABELS,
  wikiApi,
  type Campaign,
  type LocationTreeNode,
  type WikiPageStatus,
} from "@/lib/api";
import { useAsyncData } from "@/lib/use-async";

/** Indentation step per level, in px — must match the .rl-tree-guide offsets. */
const STEP = 22;

/** The coarse containers get the category colour; the rest stay muted, so a
 * screen full of buildings never turns brown. */
const COARSE_TYPES = new Set(["world", "continent", "region"]);

export function LocationTree({
  campaign,
  status,
  q,
  refreshKey = 0,
}: {
  campaign: Campaign;
  /** Status filter, applied locally so the hierarchy stays intact. */
  status?: WikiPageStatus | "";
  /** Search text, applied locally and kept matched with its ancestors. */
  q?: string;
  /** Bump to refetch (a page was just created above this panel). */
  refreshKey?: number;
}) {
  const { data, error, loading } = useAsyncData<LocationTreeNode[]>(
    (t) => wikiApi.locationTree(t, campaign.id),
    [campaign.id, refreshKey],
  );

  // Expanded by default: the hierarchy is the point of the screen. The set
  // holds what the DM closed, so a refetch never re-collapses the tree.
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());

  const tree = useMemo(() => data ?? [], [data]);
  const needle = (q ?? "").trim().toLowerCase();
  const filtered = Boolean(status) || needle.length > 0;

  /** A node the filters accept on its own account (not as someone's parent). */
  const matches = useMemo(
    () => (node: LocationTreeNode) => {
      if (status && node.status !== status) return false;
      if (!needle) return true;
      return (
        node.title.toLowerCase().includes(needle) ||
        node.slug.toLowerCase().includes(needle)
      );
    },
    [status, needle],
  );

  const visible = useMemo(() => prune(tree, matches), [tree, matches]);
  const total = countNodes(tree);
  const shown = countNodes(visible);
  const branches = useMemo(() => prune(tree, (node) => node.children.length > 0), [tree]);
  const branchIds = useMemo(() => collectIds(branches), [branches]);
  const allClosed = branchIds.length > 0 && branchIds.every((id) => collapsed.has(id));

  function toggle(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const meta =
    total === 0
      ? undefined
      : filtered
        ? shown + " of " + total + (total === 1 ? " place" : " places")
        : total + (total === 1 ? " place" : " places") +
          " · " + tree.length + " at the top level";

  return (
    <RlPanel>
      <RlPanelHead
        eyebrow="Places"
        meta={meta}
        action={
          branchIds.length > 0 ? (
            <RlButton
              variant="ghost"
              size="sm"
              onClick={() => setCollapsed(allClosed ? new Set() : new Set(branchIds))}
            >
              {allClosed ? "Expand all" : "Collapse all"}
            </RlButton>
          ) : undefined
        }
      />

      {loading ? (
        <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
          Loading places…
        </p>
      ) : error ? (
        <div className="border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
          <RlAlert tone="error">{error}</RlAlert>
        </div>
      ) : visible.length === 0 ? (
        <RlEmptyState
          icon="pin"
          tint="place"
          title={total === 0 ? "No places yet" : "Nothing matches"}
        >
          {total === 0
            ? "Places appear once a session summary is distilled and you confirm the proposed changes on the session page."
            : "Clear the status filter or the search to see the whole map again."}
        </RlEmptyState>
      ) : (
        <div>
          {visible.map((node) => (
            <LocationBranch
              key={node.id}
              node={node}
              campaignId={campaign.id}
              depth={0}
              collapsed={collapsed}
              onToggle={toggle}
              matches={matches}
              filtered={filtered}
            />
          ))}
        </div>
      )}
    </RlPanel>
  );
}

// ------------------------------------------------------------------- pieces

/** One row of the tree plus its subtree. Exported so the row chrome can be
 * reused (and rendered in isolation) by other place-aware screens. */
export function LocationBranch({
  node,
  campaignId,
  depth,
  collapsed,
  onToggle,
  matches,
  filtered,
}: {
  node: LocationTreeNode;
  campaignId: string;
  depth: number;
  collapsed: ReadonlySet<string>;
  onToggle: (id: string) => void;
  matches: (node: LocationTreeNode) => boolean;
  filtered: boolean;
}) {
  const hasChildren = node.children.length > 0;
  const open = !collapsed.has(node.id);
  const typeLabel = LOCATION_TYPE_LABELS[node.location_type] ?? node.location_type;
  const tint: RlTint = COARSE_TYPES.has(node.location_type) ? "place" : "muted";

  const sub = [
    node.slug,
    "updated " + fmtRelative(node.updated_at),
    hasChildren ? node.children.length + " inside" : null,
    node.unresolved_region ? 'region "' + node.unresolved_region + '" has no page' : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <div
        className={"rl-tree-row" + (filtered && !matches(node) ? " rl-tree-row--context" : "")}
        style={{ paddingLeft: 12 + depth * STEP }}
      >
        {Array.from({ length: depth }, (_, level) => (
          <span key={level} className="rl-tree-guide" style={{ left: 12 + level * STEP + 11 }} aria-hidden="true" />
        ))}

        {hasChildren ? (
          <button
            type="button"
            className="rl-tree-toggle"
            aria-expanded={open}
            aria-label={(open ? "Collapse " : "Expand ") + node.title}
            onClick={() => onToggle(node.id)}
          >
            <RlIcon
              name="chevron"
              size={14}
              className={"rl-tree-toggle-icon" + (open ? " rl-tree-toggle-icon--open" : "")}
            />
          </button>
        ) : (
          // keeps every row of a level aligned, with or without a toggle
          <span className="rl-tree-toggle rl-tree-toggle--empty" aria-hidden="true" />
        )}

        <Link href={"/campaigns/" + campaignId + "/pages/" + node.id} className="rl-tree-link">
          <RlIconChip name="pin" tint="place" size={34} iconSize={17} />
          <span className="min-w-0 flex-1">
            <span className="rl-list-row-title block truncate">{node.title}</span>
            <span className="rl-list-row-sub block truncate">{sub}</span>
          </span>
          <RlTag tint={tint}>{typeLabel}</RlTag>
          <span className="hidden shrink-0 sm:inline-flex">
            <PageStatusBadge status={node.status} />
          </span>
          <span className="hidden shrink-0 lg:inline-flex">
            <VisibilityBadge visibility={node.visibility} />
          </span>
          <RlIcon name="chevron" size={16} className="rl-chevron" />
        </Link>
      </div>

      {hasChildren && open
        ? node.children.map((child) => (
            <LocationBranch
              key={child.id}
              node={child}
              campaignId={campaignId}
              depth={depth + 1}
              collapsed={collapsed}
              onToggle={onToggle}
              matches={matches}
              filtered={filtered}
            />
          ))
        : null}
    </>
  );
}

// --------------------------------------------------------------- breadcrumb

/**
 * Where a place sits, as the chain of the places that contain it.
 *
 * The page itself is not repeated: it is the heading right below this line, so
 * only the ancestors render, each of them a link up the tree. A root has no
 * chain and renders nothing.
 */
export function LocationBreadcrumb({
  campaignId,
  path,
}: {
  campaignId: string;
  /** The chain from the top level down to the page's parent. */
  path: LocationTreeNode[];
}) {
  if (path.length === 0) return null;
  return (
    <nav className="rl-breadcrumb" aria-label="Contained in">
      {path.map((node, index) => (
        <span key={node.id} className="rl-breadcrumb-item">
          {index > 0 ? (
            <span className="rl-breadcrumb-sep" aria-hidden="true">
              ›
            </span>
          ) : null}
          <Link href={"/campaigns/" + campaignId + "/pages/" + node.id}>{node.title}</Link>
        </span>
      ))}
    </nav>
  );
}

/** The chain of ancestors of a page, top level first (empty for a root). */
export function findLocationPath(
  nodes: LocationTreeNode[],
  pageId: string,
): LocationTreeNode[] {
  for (const node of nodes) {
    if (node.id === pageId) return [];
    const below = findLocationPath(node.children, pageId);
    if (below.length > 0 || node.children.some((child) => child.id === pageId)) {
      return [node, ...below];
    }
  }
  return [];
}

// ------------------------------------------------------------------ helpers

/**
 * Keep every node the filters accept, plus the ancestors needed to hold it in
 * place: searching "Locanda" must show the tavern *under* Fatumastra and
 * Luxastra, or the hierarchy would vanish exactly when it is being used.
 * A matching node keeps its whole subtree — finding a city should show the
 * places inside it.
 */
function prune(
  nodes: LocationTreeNode[],
  matches: (node: LocationTreeNode) => boolean,
): LocationTreeNode[] {
  const kept: LocationTreeNode[] = [];
  for (const node of nodes) {
    if (matches(node)) {
      kept.push(node);
      continue;
    }
    const children = prune(node.children, matches);
    if (children.length > 0) kept.push({ ...node, children });
  }
  return kept;
}

function countNodes(nodes: LocationTreeNode[]): number {
  return nodes.reduce((sum, node) => sum + 1 + countNodes(node.children), 0);
}

function collectIds(nodes: LocationTreeNode[]): string[] {
  const ids: string[] = [];
  for (const node of nodes) {
    if (node.children.length > 0) {
      ids.push(node.id);
      ids.push(...collectIds(node.children));
    }
  }
  return ids;
}

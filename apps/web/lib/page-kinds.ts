// How each wiki category presents itself: one glyph and one colour, defined
// once so the sidebar, the overview cards and the wiki lists never disagree.
//
// Colours are the design system's category tints (see app/globals.css); a new
// content type means a new --rl-cat-* token, not a one-off hex.

import type { WikiPageKind } from "./api";
import type { RlIconName, RlTint } from "@/components/ravenlore";

export const PAGE_KIND_ICON: Record<WikiPageKind, RlIconName> = {
  character: "user",
  location: "pin",
  faction: "flag",
  item: "gem",
  quest: "scroll",
  event: "spark",
};

export const PAGE_KIND_TINT: Record<WikiPageKind, RlTint> = {
  character: "npc",
  location: "place",
  faction: "faction",
  item: "item",
  quest: "neutral",
  event: "ink",
};

/** Route of a category's index page inside a campaign. */
export function worldPath(campaignId: string, kind?: WikiPageKind): string {
  return kind ? `/campaigns/${campaignId}/world/${kind}` : `/campaigns/${campaignId}/world`;
}

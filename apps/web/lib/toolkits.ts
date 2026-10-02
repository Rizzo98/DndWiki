// The DM toolkits: the tools only a campaign's Dungeon Master sees.
//
// Defined once, like the wiki categories in page-kinds.ts, so the sidebar and
// the narrow-screen tab strip cannot disagree about what exists or what it is
// called. It is a LIST because the toolkits are a shelf rather than a feature:
// 'Plan' is the first tool on it, and the next one is a new entry here plus the
// page it points at.
//
// Every toolkit is DM-only. That is enforced server-side (each route checks the
// campaign role or the Keycloak 'dev' realm role), so hiding the entries is
// about not showing a player a door they cannot open - not about the security
// itself.

import type { RlIconName } from "@/components/ravenlore";

export interface Toolkit {
  /** Route inside the campaign workspace (the campaign id is prepended). */
  href: string;
  label: string;
  icon: RlIconName;
  /** One line explaining what the tool is for (lists, empty states). */
  blurb: string;
}

export const DM_TOOLKITS: Toolkit[] = [
  {
    href: "/toolkit/plan",
    label: "Plan",
    icon: "scroll",
    blurb:
      "Write what you have in mind, then turn the notes you pick into wiki pages.",
  },
];

/** Route of a toolkit inside a campaign. */
export function toolkitPath(campaignId: string, href: string): string {
  return `/campaigns/${campaignId}${href}`;
}

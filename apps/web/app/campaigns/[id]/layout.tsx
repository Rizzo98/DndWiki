// The campaign workspace shell.
//
// Everything under /campaigns/<id> lives inside this layout: it loads the
// campaign once, hands it to the pages through CampaignProvider (so switching
// tabs does not refetch it), and — on narrow screens, where the sidebar is
// hidden — shows the workspace tabs as a scrollable strip.

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AuthGate } from "@/lib/auth";
import { PAGE_KINDS, PAGE_KIND_LABELS, campaignsApi, type Campaign } from "@/lib/api";
import { CampaignProvider } from "@/lib/campaign-context";
import { worldPath } from "@/lib/page-kinds";
import { useAsyncData } from "@/lib/use-async";
import { RlAlert } from "@/components/ravenlore";

export default function CampaignLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { id: string };
}) {
  const { data: campaign, error, loading, reload } = useAsyncData<Campaign>(
    (t) => campaignsApi.get(t, params.id),
    [params.id],
  );

  return (
    <AuthGate>
      {loading ? (
        <div className="space-y-6" aria-hidden="true">
          <p className="sr-only">Loading campaign…</p>
          <div className="rl-skeleton h-56" />
          <div className="rl-skeleton h-24" />
        </div>
      ) : error || !campaign ? (
        <RlAlert tone="error">{error ?? "Campaign not found"}</RlAlert>
      ) : (
        <CampaignProvider campaign={campaign} reload={reload}>
          <CampaignTabStrip campaign={campaign} />
          {children}
        </CampaignProvider>
      )}
    </AuthGate>
  );
}

/** Narrow screens only: the workspace tabs the sidebar carries on desktop. */
function CampaignTabStrip({ campaign }: { campaign: Campaign }) {
  const pathname = usePathname();
  const base = "/campaigns/" + campaign.id;
  const isDm = campaign.my_role === "dm";
  const tabs = [
    { href: base, label: "Overview", exact: true },
    { href: base + "/sessions", label: "Sessions" },
    { href: base + "/timeline", label: "Timeline" },
    ...(isDm
      ? [
          { href: base + "/members", label: "Members" },
          { href: base + "/invites", label: "Invites" },
        ]
      : []),
  ];

  return (
    <nav aria-label="Campaign sections" className="rl-tabstrip mb-6 lg:hidden">
      {tabs.map((t) => {
        const active = t.exact ? pathname === t.href : pathname.startsWith(t.href);
        return (
          <Link key={t.href} href={t.href} className={"rl-tab" + (active ? " rl-tab--active" : "")}>
            {t.label}
          </Link>
        );
      })}
      {PAGE_KINDS.map((kind) => {
        const href = worldPath(campaign.id, kind);
        return (
          <Link
            key={kind}
            href={href}
            className={"rl-tab" + (pathname.startsWith(href) ? " rl-tab--active" : "")}
          >
            {PAGE_KIND_LABELS[kind]}
          </Link>
        );
      })}
    </nav>
  );
}

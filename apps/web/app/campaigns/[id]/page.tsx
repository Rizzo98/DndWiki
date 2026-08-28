// /campaigns/[id] — campaign workspace (overview, members, invites, sessions, wiki, timeline).

"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui";
import { AuthGate } from "@/lib/auth";
import { campaignsApi, type Campaign } from "@/lib/api";
import { useAsyncData } from "@/lib/use-async";
import { OverviewTab } from "@/components/campaign/overview";
import { MembersTab } from "@/components/campaign/members";
import { InvitesTab } from "@/components/campaign/invites";
import { SessionsTab } from "@/components/campaign/sessions";
import { WikiTab } from "@/components/campaign/wiki";
import { TimelineTab } from "@/components/campaign/timeline";

type TabId = "overview" | "members" | "invites" | "sessions" | "wiki" | "timeline";

export default function CampaignPage({ params }: { params: { id: string } }) {
  const { data: campaign, error, loading, reload } = useAsyncData<Campaign>((t) => campaignsApi.get(t, params.id), [params.id]);
  const [tab, setTab] = useState<TabId>("overview");

  // Deep links (?tab=timeline) open the requested tab; runs client-side only
  // so server rendering never sees window.
  useEffect(() => {
    const t = new URLSearchParams(window.location.search).get("tab") as TabId | null;
    if (t && ["overview", "wiki", "sessions", "timeline", "members", "invites"].includes(t)) {
      setTab(t);
    }
  }, []);

  if (loading) return <p className="text-sm text-slate-400">Loading campaign…</p>;
  if (error || !campaign) return <p className="text-sm text-red-300">{error ?? "Campaign not found"}</p>;

  const isDm = campaign.my_role === "dm";
  const tabs: { id: TabId; label: string; visible: boolean }[] = [
    { id: "overview", label: "Overview", visible: true },
    { id: "wiki", label: "Wiki", visible: true },
    { id: "sessions", label: "Sessions", visible: true },
    { id: "timeline", label: "Timeline", visible: true },
    { id: "members", label: "Members", visible: isDm },
    { id: "invites", label: "Invites", visible: isDm },
  ];

  return (
    <AuthGate>
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold">{campaign.name}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-400">
            <span className="font-mono text-xs">{campaign.slug}</span>
            <Badge tone={isDm ? "amber" : "blue"}>{campaign.my_role ?? "member"}</Badge>
            <Badge tone={campaign.status === "active" ? "green" : "slate"}>{campaign.status}</Badge>
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-slate-800">
        {tabs
          .filter((t) => t.visible)
          .map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded-t-lg px-4 py-2 text-sm font-medium transition ${
                tab === t.id ? "border border-b-0 border-slate-800 bg-slate-900 text-slate-100" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
      </div>

      {tab === "overview" && <OverviewTab campaign={campaign} onChanged={reload} />}
      {tab === "members" && <MembersTab campaign={campaign} />}
      {tab === "invites" && <InvitesTab campaign={campaign} />}
      {tab === "sessions" && <SessionsTab campaign={campaign} />}
      {tab === "wiki" && <WikiTab campaign={campaign} />}
      {tab === "timeline" && <TimelineTab campaign={campaign} />}
    </div>
    </AuthGate>
  );
}

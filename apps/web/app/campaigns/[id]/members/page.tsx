// /campaigns/[id]/members — the roster (DM only: players never see each other's rows).

"use client";

import { CampaignPageHead } from "@/components/campaign/section";
import { MembersTab } from "@/components/campaign/members";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignMembersPage() {
  const { campaign } = useCampaign();

  return (
    <>
      <CampaignPageHead
        eyebrow={campaign.name}
        title="Members"
        description="Who sits at this table, and which character each of them plays."
      />
      <MembersTab campaign={campaign} />
    </>
  );
}

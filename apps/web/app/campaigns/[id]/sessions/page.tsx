// /campaigns/[id]/sessions — every recording of the campaign and its pipeline state.

"use client";

import { CampaignPageHead } from "@/components/campaign/section";
import { SessionsTab } from "@/components/campaign/sessions";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignSessionsPage() {
  const { campaign } = useCampaign();

  return (
    <>
      <CampaignPageHead
        eyebrow={campaign.name}
        title="Sessions"
        description="Upload a recording and the pipeline transcribes it, writes the summary and proposes the wiki changes it implies."
      />
      <SessionsTab campaign={campaign} />
    </>
  );
}

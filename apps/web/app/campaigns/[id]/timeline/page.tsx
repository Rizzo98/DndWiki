// /campaigns/[id]/timeline — the campaign's history in the order it happened.

"use client";

import { CampaignPageHead } from "@/components/campaign/section";
import { TimelineTab } from "@/components/campaign/timeline";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignTimelinePage() {
  const { campaign } = useCampaign();

  return (
    <>
      <CampaignPageHead
        eyebrow={campaign.name}
        title="Timeline"
        description="What happened, and when — assembled from the sessions you confirmed."
      />
      <TimelineTab campaign={campaign} />
    </>
  );
}

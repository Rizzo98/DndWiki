// /campaigns/[id]/world — every wiki page of the campaign, all categories.

"use client";

import { WikiTab } from "@/components/campaign/wiki";
import { CampaignPageHead } from "@/components/campaign/section";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignWorldPage() {
  const { campaign } = useCampaign();

  return (
    <>
      <CampaignPageHead
        eyebrow="The world"
        title="All pages"
        description="Everything the campaign documents so far, newest first."
      />
      <WikiTab campaign={campaign} />
    </>
  );
}

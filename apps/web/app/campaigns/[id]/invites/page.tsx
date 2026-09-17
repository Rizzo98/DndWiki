// /campaigns/[id]/invites — one-time join links (DM only).

"use client";

import { CampaignPageHead } from "@/components/campaign/section";
import { InvitesTab } from "@/components/campaign/invites";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignInvitesPage() {
  const { campaign } = useCampaign();

  return (
    <>
      <CampaignPageHead
        eyebrow={campaign.name}
        title="Invites"
        description="Hand a one-time token to a player and they join the table with the role you chose."
      />
      <InvitesTab campaign={campaign} />
    </>
  );
}

// /campaigns/[id]/world/[kind] — the wiki, scoped to one category.

"use client";

import { WikiTab } from "@/components/campaign/wiki";
import { CampaignPageHead } from "@/components/campaign/section";
import { PAGE_KINDS, PAGE_KIND_LABELS, type WikiPageKind } from "@/lib/api";
import { useCampaign } from "@/lib/campaign-context";

export default function CampaignWorldKindPage({ params }: { params: { id: string; kind: string } }) {
  const { campaign } = useCampaign();
  const kind = params.kind as WikiPageKind;

  if (!PAGE_KINDS.includes(kind)) {
    return (
      <CampaignPageHead
        eyebrow="The world"
        title="Unknown category"
        description={`"${params.kind}" is not one of the campaign's page categories.`}
      />
    );
  }

  return (
    <>
      <CampaignPageHead
        eyebrow="The world"
        title={PAGE_KIND_LABELS[kind]}
        description={`Every ${PAGE_KIND_LABELS[kind].toLowerCase().replace(/s$/, "")} page of ${campaign.name}.`}
      />
      <WikiTab campaign={campaign} kind={kind} />
    </>
  );
}

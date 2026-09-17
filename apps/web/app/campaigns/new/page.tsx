// /campaigns/new — dedicated campaign creation page: details, session language
// and the starting roster. On success the DM lands on the new campaign.

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { AuthGate } from "@/lib/auth";
import { CreateCampaignForm } from "@/components/campaign/create";

export default function NewCampaignPage() {
  const router = useRouter();

  return (
    <AuthGate>
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <Link href="/campaigns" className="text-sm text-[color:var(--rl-text-on-parchment-muted)] transition hover:text-[color:var(--rl-text-on-parchment-primary)]">
          ← Back to campaigns
        </Link>
        <h1 className="rl-title mt-2 text-3xl">New campaign</h1>
        <p className="mt-1 text-sm text-[color:var(--rl-text-on-parchment-muted)]">
          You become the Dungeon Master of the campaign you create. Add the players at your
          table now — you can always manage the roster later from the campaign workspace.
        </p>
      </div>

      <CreateCampaignForm onCreated={(campaign) => router.push(`/campaigns/${campaign.id}`)} />
    </div>
    </AuthGate>
  );
}

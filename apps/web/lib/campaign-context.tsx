// Campaign context: the campaign workspace fetches the campaign once (in the
// [id] layout) and every page underneath reads it from here, so navigating
// between tabs does not refetch it and the DM controls always see fresh data
// (reload() after editing, uploading a cover, archiving…).

"use client";

import { createContext, useContext, type ReactNode } from "react";
import type { Campaign } from "./api";

interface CampaignContextValue {
  campaign: Campaign;
  /** Refetch the campaign (after a DM edit, a cover upload, archive…). */
  reload: () => void;
}

const CampaignContext = createContext<CampaignContextValue | null>(null);

export function CampaignProvider({
  campaign,
  reload,
  children,
}: {
  campaign: Campaign;
  reload: () => void;
  children: ReactNode;
}) {
  return <CampaignContext.Provider value={{ campaign, reload }}>{children}</CampaignContext.Provider>;
}

/** The campaign of the workspace this page lives in. */
export function useCampaign(): CampaignContextValue {
  const ctx = useContext(CampaignContext);
  if (!ctx) throw new Error("useCampaign must be used inside /campaigns/[id]");
  return ctx;
}

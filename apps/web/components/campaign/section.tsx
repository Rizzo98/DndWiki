// Page head shared by the campaign workspace pages: eyebrow, serif title,
// one line of context, optional action on the right.

import type { ReactNode } from "react";

export function CampaignPageHead({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <span className="rl-eyebrow">{eyebrow}</span>
        <h1 className="rl-title mt-1 text-3xl">{title}</h1>
        {description ? <p className="rl-body mt-1.5 max-w-[72ch]">{description}</p> : null}
      </div>
      {action}
    </div>
  );
}

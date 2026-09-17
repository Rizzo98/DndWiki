// /campaigns/[id] — the campaign home.
//
// The landing page of one campaign: cover art and title, what the campaign
// holds right now (pages, sessions, players, events), the party, and the pages
// the table touched most recently. The DM's edit/archive console lives at the
// bottom, out of the way of reading.

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { OverviewTab } from "@/components/campaign/overview";
import { PageStatusBadge, SessionStatusBadge, fmtDuration } from "@/components/ui";
import {
  RlAlert,
  RlButton,
  RlButtonLink,
  RlCard,
  RlEmptyState,
  RlIcon,
  RlIconChip,
  RlListRow,
  RlPanel,
  RlPanelHead,
  RlSectionHead,
  RlStat,
  RlStatRow,
  RlTag,
  fmtRelative,
  rlButtonClass,
  rlTintVars,
  sessionStamp,
  sessionStatusTint,
} from "@/components/ravenlore";
import { useAuth } from "@/lib/auth";
import { useCampaign } from "@/lib/campaign-context";
import {
  PAGE_KINDS,
  PAGE_KIND_LABELS,
  PAGE_KIND_TITLES,
  campaignLanguageLabel,
  campaignsApi,
  objectUrl,
  sessionLabel,
  sessionsApi,
  wikiApi,
  type Campaign,
  type CampaignMember,
  type PageSummary,
  type Session,
  type TimelineEvent,
} from "@/lib/api";
import { PAGE_KIND_ICON, PAGE_KIND_TINT, worldPath } from "@/lib/page-kinds";
import { errMessage, useAsyncData } from "@/lib/use-async";

/** One-line description of what each category collects, for the world panel. */
const KIND_BLURB: Record<string, string> = {
  character: "People, allies and villains",
  location: "Cities, regions and ruins",
  faction: "Orders, houses and guilds",
  item: "Artifacts and gear",
  quest: "Open threads and promises",
  event: "What happened, and when",
};

/** Old deep links (?tab=timeline) still land on the right page. */
const LEGACY_TABS: Record<string, string> = {
  sessions: "sessions",
  timeline: "timeline",
  members: "members",
  invites: "invites",
  wiki: "world",
};

export default function CampaignOverviewPage() {
  const { campaign, reload } = useCampaign();
  const router = useRouter();
  const isDm = campaign.my_role === "dm";

  const { data: members } = useAsyncData<CampaignMember[]>((t) => campaignsApi.members(t, campaign.id), [campaign.id]);
  const { data: sessions } = useAsyncData<Session[]>((t) => sessionsApi.list(t, campaign.id), [campaign.id]);
  const { data: pages } = useAsyncData<PageSummary[]>((t) => wikiApi.pages(t, campaign.id, { limit: 200 }), [campaign.id]);
  const { data: events } = useAsyncData<TimelineEvent[]>((t) => wikiApi.timeline(t, campaign.id), [campaign.id]);

  useEffect(() => {
    const tab = new URLSearchParams(window.location.search).get("tab");
    const target = tab ? LEGACY_TABS[tab] : undefined;
    if (target) router.replace("/campaigns/" + campaign.id + "/" + target);
  }, [campaign.id, router]);

  const roster = members ?? [];
  const sessionList = sessions ?? [];
  const pageList = pages ?? [];
  const dmRow = roster.find((m) => m.role === "dm");
  const players = roster.filter((m) => m.role !== "dm");

  const recentPages = useMemo(
    () => [...pageList].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 6),
    [pages],
  );
  const recentSessions = useMemo(
    () => [...sessionList].sort((a, b) => sessionStamp(b) - sessionStamp(a)).slice(0, 4),
    [sessions],
  );
  const countByKind = useMemo(() => {
    const counts: Partial<Record<string, number>> = {};
    for (const page of pageList) counts[page.kind] = (counts[page.kind] ?? 0) + 1;
    return counts;
  }, [pages]);

  // Presigned URLs carry the docker-internal minio host, which the browser
  // cannot resolve: they are served through the object proxy like every other
  // uploaded asset (portraits, avatars, recordings).
  const coverUrl = objectUrl(campaign.cover_url);

  const lastPlayed = recentSessions[0] ? recentSessions[0].recorded_at ?? recentSessions[0].created_at : null;
  const eyebrow =
    sessionList.length > 0
      ? sessionList.length +
        (sessionList.length === 1 ? " session" : " sessions") +
        (lastPlayed ? " · last played " + fmtRelative(lastPlayed) : "")
      : "No sessions recorded yet";

  return (
    <div className="space-y-7">
      <section className={"rl-hero" + (coverUrl ? " rl-hero--cover" : "")}>
        {coverUrl ? <div className="rl-hero-cover" style={{ backgroundImage: "url(" + coverUrl + ")" }} /> : null}
        <div className="rl-hero-overlay">
          <div className="flex flex-wrap items-end justify-between gap-6">
            <div className="min-w-0">
              <p className="rl-hero-eyebrow">{eyebrow}</p>
              <h1 className="rl-hero-title">{campaign.name}</h1>
              <p className="rl-hero-desc">
                {campaign.description ?? "No description yet — the DM can add one from the campaign settings below."}
              </p>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <RlTag onDark tint={campaign.status === "active" ? "neutral" : "muted"}>
                  {campaign.status === "active" ? "Active campaign" : "Archived"}
                </RlTag>
                <RlTag onDark tint={isDm ? "faction" : "npc"}>
                  {isDm ? "Dungeon Master" : "Player"}
                </RlTag>
                <RlTag onDark tint="muted">
                  {campaignLanguageLabel(campaign.language)}
                </RlTag>
              </div>
            </div>
            {isDm ? <CoverControls campaign={campaign} onChanged={reload} /> : null}
          </div>
        </div>
      </section>

      <RlStatRow>
        <RlStat icon="scroll" tint="place" value={pageList.length} label="Wiki pages" />
        <RlStat icon="mic" tint="item" value={sessionList.length} label="Sessions" />
        <RlStat icon="users" tint="npc" value={players.length} label="Players" />
        <RlStat icon="clock" tint="ink" value={events ? events.length : "—"} label="Timeline events" />
      </RlStatRow>

      <div className="grid gap-7 lg:grid-cols-3">
        <div className="space-y-7 lg:col-span-2">
          <section className="space-y-4">
            <RlSectionHead
              eyebrow="Recently touched"
              meta={pageList.length > 0 ? pageList.length + (pageList.length === 1 ? " page" : " pages") : undefined}
              action={
                pageList.length > 0 ? (
                  <RlButtonLink href={worldPath(campaign.id)} variant="outline" size="sm">
                    All pages
                  </RlButtonLink>
                ) : null
              }
            />
            {recentPages.length === 0 ? (
              <RlEmptyState icon="scroll" title="No pages yet">
                The wiki fills up as you confirm session summaries: the pipeline proposes the characters, places and
                factions it heard, and you decide what enters the campaign.
              </RlEmptyState>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2">
                {recentPages.map((page) => (
                  <PageCard key={page.id} campaign={campaign} page={page} />
                ))}
              </div>
            )}
          </section>

          <RlPanel>
            <RlPanelHead
              eyebrow="Latest sessions"
              action={
                sessionList.length > 0 ? (
                  <RlButtonLink href={"/campaigns/" + campaign.id + "/sessions"} variant="ghost" size="sm">
                    All sessions
                  </RlButtonLink>
                ) : undefined
              }
            />
            {recentSessions.length === 0 ? (
              <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
                No recordings yet. Upload one from the Sessions tab and the pipeline takes it from there.
              </p>
            ) : (
              recentSessions.map((session) => (
                <RlListRow
                  key={session.id}
                  href={"/campaigns/" + campaign.id + "/sessions/" + session.id}
                  icon="mic"
                  tint={sessionStatusTint(session.status)}
                  title={sessionLabel(session)}
                  sub={
                    fmtRelative(session.recorded_at ?? session.created_at) +
                    (session.duration_sec ? " · " + fmtDuration(session.duration_sec) : "")
                  }
                  badge={<SessionStatusBadge status={session.status} />}
                />
              ))
            )}
          </RlPanel>

          {isDm ? (
            <section className="space-y-4">
              <RlSectionHead eyebrow="Dungeon Master" title="Campaign settings" meta="Details, language and status" />
              <OverviewTab campaign={campaign} onChanged={reload} />
            </section>
          ) : null}
        </div>

        <aside className="space-y-6">
          <RlPanel>
            <RlPanelHead eyebrow="The party" meta={roster.length + " at the table"} />
            {roster.length === 0 ? (
              <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
                Nobody at the table yet.
              </p>
            ) : (
              <>
                {dmRow ? <PartyRow member={dmRow} /> : null}
                {players.map((member) => (
                  <PartyRow key={member.id} member={member} />
                ))}
              </>
            )}
          </RlPanel>

          <RlPanel>
            <RlPanelHead eyebrow="The world" meta={pageList.length + (pageList.length === 1 ? " page" : " pages")} />
            {PAGE_KINDS.map((kind) => {
              const count = countByKind[kind] ?? 0;
              return (
                <RlListRow
                  key={kind}
                  href={worldPath(campaign.id, kind)}
                  icon={PAGE_KIND_ICON[kind]}
                  tint={PAGE_KIND_TINT[kind]}
                  title={PAGE_KIND_LABELS[kind]}
                  sub={count === 0 ? "nothing yet" : KIND_BLURB[kind]}
                  trailing={count}
                />
              );
            })}
          </RlPanel>
        </aside>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- pieces

/** DM cover-art control, sitting on the hero it edits. */
function CoverControls({ campaign, onChanged }: { campaign: Campaign; onChanged: () => void }) {
  const { token } = useAuth();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function pick(file: File | null) {
    if (!token || !file) return;
    setBusy(true);
    setError(null);
    try {
      await campaignsApi.uploadCover(token, campaign.id, file);
      onChanged();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  async function remove() {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await campaignsApi.deleteCover(token, campaign.id);
      onChanged();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <div className="flex items-center gap-2">
        <label className={rlButtonClass("on-dark", "rl-btn--sm", "cursor-pointer")}>
          <RlIcon name="image" size={15} />
          {busy ? "Uploading…" : campaign.cover_url ? "Change cover" : "Add cover"}
          <input
            ref={input}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="sr-only"
            disabled={busy}
            onChange={(e) => pick(e.target.files?.[0] ?? null)}
          />
        </label>
        {campaign.cover_url ? (
          <RlButton variant="on-dark" size="sm" onClick={remove} disabled={busy}>
            Remove
          </RlButton>
        ) : null}
      </div>
      {error ? <RlAlert tone="error">{error}</RlAlert> : null}
    </div>
  );
}

function PartyRow({ member }: { member: CampaignMember }) {
  const isDm = member.role === "dm";
  const title = isDm ? member.player_name || "Dungeon Master" : member.character_name || member.player_name || "Unnamed";
  const sub = isDm
    ? "Dungeon Master"
    : member.player_name
      ? "played by " + member.player_name
      : "player at the table";
  return (
    <div className="rl-list-row">
      <span className="rl-avatar rl-avatar--light">{title.trim().charAt(0) || "?"}</span>
      <span className="min-w-0 flex-1">
        <span className="rl-list-row-title block truncate">{title}</span>
        <span className="rl-list-row-sub block truncate">{sub}</span>
      </span>
      {isDm ? <RlTag tint="faction">DM</RlTag> : null}
    </div>
  );
}

function PageCard({ campaign, page }: { campaign: Campaign; page: PageSummary }) {
  const tint = PAGE_KIND_TINT[page.kind];
  return (
    <RlCard href={"/campaigns/" + campaign.id + "/pages/" + page.id} className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-3">
        <RlIconChip name={PAGE_KIND_ICON[page.kind]} tint={tint} size={38} iconSize={19} />
        <PageStatusBadge status={page.status} />
      </div>
      <span className="rl-eyebrow rl-tint mt-3.5" style={rlTintVars(tint)}>
        {PAGE_KIND_TITLES[page.kind]}
      </span>
      <h3 className="rl-card-title mt-1.5">{page.title}</h3>
      <p className="rl-card-meta mt-auto pt-3">Updated {fmtRelative(page.updated_at)}</p>
    </RlCard>
  );
}

// /campaigns — the campaigns landing page.
//
// One question drives the whole screen: "which table am I at, and what does
// each one need from me?" So every card carries its latest session state, the
// dark rail is the invite door, and the parchment rail answers "where was I?"
// with the most recent recordings across all campaigns.
//
// Creating a campaign is a page of its own (/campaigns/new); this page links
// there rather than hosting the form.
//
// Composition is this page's call. The chrome — hero, cards, tags, stat tiles,
// list rows, the dark panel — comes from the Ravenlore design system
// (components/ravenlore.tsx + the tokens in app/globals.css).

"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignLanguageLabel, campaignsApi, sessionLabel, sessionsApi, type Campaign, type Session } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";
import { fmtDuration } from "@/components/ui";
import {
  RlAlert,
  RlButton,
  RlButtonLink,
  RlCard,
  RlChip,
  RlDarkPanel,
  RlEmptyState,
  RlField,
  RlIcon,
  RlIconChip,
  RlListRow,
  RlPanel,
  RlPanelHead,
  RlSearchInput,
  RlSectionHead,
  RlStat,
  RlStatRow,
  RlTag,
  RlTextInput,
  fmtRelative,
  rlTintVars,
  sessionStamp,
  sessionStatusTint,
} from "@/components/ravenlore";

type StatusFilter = "all" | "active" | "archived";

/** How many recordings the "recent activity" rail lists. */
const ACTIVITY_LIMIT = 5;

export default function CampaignsPage() {
  const { token, isDm } = useAuth();
  const { data: campaigns, error, loading, reload } = useAsyncData<Campaign[]>((t) => campaignsApi.list(t));

  // campaign id -> its sessions. Fetched after the campaign list so the cards
  // can show what the pipeline is doing; a campaign whose sessions fail to load
  // simply falls back to "Loading sessions…" rather than breaking the page.
  const [sessionsByCampaign, setSessionsByCampaign] = useState<Record<string, Session[]>>({});

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [inviteToken, setInviteToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !campaigns || campaigns.length === 0) return;
    let cancelled = false;
    Promise.all(
      campaigns.map((c) =>
        sessionsApi
          .list(token, c.id)
          .then((sessions) => [c.id, sessions] as const)
          .catch(() => [c.id, [] as Session[]] as const),
      ),
    ).then((entries) => {
      if (!cancelled) setSessionsByCampaign(Object.fromEntries(entries));
    });
    return () => {
      cancelled = true;
    };
  }, [token, campaigns]);

  async function acceptInvite(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setInviteError(null);
    setNotice(null);
    try {
      const campaign = await campaignsApi.acceptInvite(token, inviteToken.trim());
      setNotice('Joined "' + campaign.name + '".');
      setInviteToken("");
      reload();
    } catch (err) {
      setInviteError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const stats = useMemo(() => {
    const list = campaigns ?? [];
    const active = list.filter((c) => c.status === "active").length;
    const dm = list.filter((c) => c.my_role === "dm").length;
    const values = Object.values(sessionsByCampaign);
    return {
      total: list.length,
      active,
      dm,
      sessions: values.reduce((n, list) => n + list.length, 0),
      sessionsLoaded: values.length > 0,
    };
  }, [campaigns, sessionsByCampaign]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (campaigns ?? []).filter((c) => {
      if (filter !== "all" && c.status !== filter) return false;
      if (!needle) return true;
      return c.name.toLowerCase().includes(needle) || c.slug.toLowerCase().includes(needle);
    });
  }, [campaigns, query, filter]);

  const recent = useMemo(() => {
    const rows: { campaign: Campaign; session: Session }[] = [];
    for (const campaign of campaigns ?? []) {
      for (const session of sessionsByCampaign[campaign.id] ?? []) rows.push({ campaign, session });
    }
    return rows.sort((a, b) => sessionStamp(b.session) - sessionStamp(a.session)).slice(0, ACTIVITY_LIMIT);
  }, [campaigns, sessionsByCampaign]);

  const filtered = query.trim() !== "" || filter !== "all";
  const summary = filtered
    ? "Showing " + visible.length + " of " + stats.total
    : stats.total +
      (stats.total === 1 ? " campaign" : " campaigns") +
      (stats.sessionsLoaded ? " · " + stats.sessions + (stats.sessions === 1 ? " session" : " sessions") + " recorded" : "");

  const heroEyebrow = stats.total > 0 ? "Your table · " + stats.total + (stats.total === 1 ? " campaign" : " campaigns") : "Your table";
  const heroDescription = isDm
    ? "You hold the Dungeon Master realm role. Each campaign keeps its own sessions, wiki pages and timeline."
    : "The tables you play at. Open one to read its wiki, replay its sessions and follow the timeline.";

  return (
    <AuthGate>
      <div className="space-y-8">
        <section className="rl-hero">
          <div className="rl-hero-overlay">
            <div className="flex flex-wrap items-end justify-between gap-6">
              <div className="min-w-0">
                <p className="rl-hero-eyebrow">{heroEyebrow}</p>
                <h1 className="rl-hero-title">Your campaigns</h1>
                <p className="rl-hero-desc">{heroDescription}</p>
              </div>
              <RlButtonLink href="/campaigns/new">
                <RlIcon name="plus" size={16} />
                New campaign
              </RlButtonLink>
            </div>
          </div>
        </section>

        <section className="space-y-7">
          {notice ? <RlAlert tone="success">{notice}</RlAlert> : null}

          {loading ? (
            <LoadingSkeleton />
          ) : error ? (
            <RlAlert tone="error">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span>{error}</span>
                <RlButton variant="outline" size="sm" onClick={reload}>
                  Try again
                </RlButton>
              </div>
            </RlAlert>
          ) : (
            <>
              {stats.total > 0 ? (
                <RlStatRow>
                  <RlStat
                    icon="book"
                    tint="place"
                    value={stats.total}
                    label={stats.total === 1 ? "Campaign" : "Campaigns"}
                  />
                  <RlStat icon="flame" tint="neutral" value={stats.active} label="Active" />
                  <RlStat icon="crown" tint="faction" value={stats.dm} label="As Dungeon Master" />
                  <RlStat
                    icon="mic"
                    tint="item"
                    value={stats.sessionsLoaded ? stats.sessions : "—"}
                    label="Sessions recorded"
                  />
                </RlStatRow>
              ) : null}

              <div className="grid gap-7 lg:grid-cols-3">
                <div className="space-y-5 lg:col-span-2">
                  {stats.total > 0 ? (
                    <RlSectionHead
                      eyebrow="Your campaigns"
                      meta={summary}
                      action={
                        <div className="flex flex-wrap items-center gap-2">
                          <div className="w-full sm:w-56">
                            <RlSearchInput
                              value={query}
                              onChange={setQuery}
                              placeholder="Search campaigns…"
                              ariaLabel="Search campaigns"
                            />
                          </div>
                          <div className="flex items-center gap-1.5">
                            <RlChip active={filter === "all"} onClick={() => setFilter("all")}>
                              All
                            </RlChip>
                            <RlChip active={filter === "active"} onClick={() => setFilter("active")}>
                              Active
                            </RlChip>
                            <RlChip active={filter === "archived"} onClick={() => setFilter("archived")}>
                              Archived
                            </RlChip>
                          </div>
                        </div>
                      }
                    />
                  ) : null}

                  {stats.total === 0 ? (
                    <RlEmptyState
                      icon="spark"
                      tint="accent"
                      title="No campaigns yet"
                      action={
                        <RlButtonLink href="/campaigns/new">
                          <RlIcon name="plus" size={16} />
                          Create your first campaign
                        </RlButtonLink>
                      }
                    >
                      Creating one makes you its Dungeon Master. Already invited to a table? Paste the invite token
                      from your DM into the &ldquo;Accept an invite&rdquo; panel.
                    </RlEmptyState>
                  ) : visible.length === 0 ? (
                    <RlEmptyState
                      icon="search"
                      title="Nothing matches"
                      action={
                        <RlButton
                          variant="outline"
                          onClick={() => {
                            setQuery("");
                            setFilter("all");
                          }}
                        >
                          Clear filters
                        </RlButton>
                      }
                    >
                      No campaign matches the current search and filter.
                    </RlEmptyState>
                  ) : (
                    <div className="grid gap-4 sm:grid-cols-2">
                      {visible.map((campaign) => (
                        <CampaignCard
                          key={campaign.id}
                          campaign={campaign}
                          sessions={sessionsByCampaign[campaign.id]}
                        />
                      ))}
                    </div>
                  )}
                </div>

                <aside className="space-y-6">
                  <RlDarkPanel
                    title="Accept an invite"
                    icon="key"
                    note="Paste the token your Dungeon Master sent you. You join the table with the role they assigned."
                  >
                    <form onSubmit={acceptInvite} className="space-y-3">
                      <RlField label="Invite token">
                        <RlTextInput
                          required
                          value={inviteToken}
                          onChange={(e) => setInviteToken(e.target.value)}
                          placeholder="paste the token"
                          autoComplete="off"
                        />
                      </RlField>
                      {inviteError ? (
                        <RlAlert tone="error" onDark>
                          {inviteError}
                        </RlAlert>
                      ) : null}
                      <RlButton type="submit" block disabled={busy || inviteToken.trim() === ""}>
                        {busy ? "Joining…" : "Join campaign"}
                      </RlButton>
                    </form>
                  </RlDarkPanel>

                  {stats.total > 0 ? (
                    <RlPanel>
                      <RlPanelHead
                        eyebrow="Recent activity"
                        meta={stats.sessionsLoaded && stats.sessions === 0 ? "no recordings yet" : undefined}
                      />
                      {recent.length === 0 ? (
                        <p className="rl-body border-t border-[color:var(--rl-border-parchment)] px-4 py-4">
                          No session recordings yet. Upload one from a campaign&rsquo;s Sessions tab and it shows up
                          here.
                        </p>
                      ) : (
                        recent.map(({ campaign, session }) => (
                          <RlListRow
                            key={session.id}
                            href={"/campaigns/" + campaign.id + "/sessions/" + session.id}
                            icon="mic"
                            tint={sessionStatusTint(session.status)}
                            title={sessionLabel(session)}
                            sub={campaign.name + " · " + fmtRelative(session.recorded_at ?? session.created_at)}
                            trailing={session.duration_sec ? fmtDuration(session.duration_sec) : undefined}
                          />
                        ))
                      )}
                    </RlPanel>
                  ) : null}
                </aside>
              </div>
            </>
          )}
        </section>
      </div>
    </AuthGate>
  );
}

// ------------------------------------------------------------------- pieces

function CampaignCard({ campaign, sessions }: { campaign: Campaign; sessions: Session[] | undefined }) {
  const asDm = campaign.my_role === "dm";
  const ordered = sessions ? [...sessions].sort((a, b) => sessionStamp(b) - sessionStamp(a)) : undefined;
  const latest = ordered && ordered.length > 0 ? ordered[0] : undefined;

  const meta = !ordered
    ? "Loading sessions…"
    : latest
      ? ordered.length +
        (ordered.length === 1 ? " session · last played " : " sessions · last played ") +
        fmtRelative(latest.recorded_at ?? latest.created_at)
      : "No sessions recorded yet";

  return (
    <RlCard href={"/campaigns/" + campaign.id} className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-3">
        <RlIconChip name={asDm ? "crown" : "book"} tint={asDm ? "faction" : "npc"} size={40} iconSize={20} />
        <RlTag tint={campaign.status === "active" ? "accent" : "muted"}>
          {campaign.status === "active" ? "Active" : "Archived"}
        </RlTag>
      </div>

      <span className="rl-eyebrow rl-tint mt-4" style={rlTintVars(asDm ? "faction" : "npc")}>
        {asDm ? "Dungeon Master" : "Player"}
      </span>
      <h3 className="rl-card-title mt-1.5">{campaign.name}</h3>

      {campaign.description ? (
        <p className="rl-body mt-2 line-clamp-2">{campaign.description}</p>
      ) : (
        <p className="rl-body mt-2 italic">No description yet.</p>
      )}

      <div className="mt-auto pt-4">
        <hr className="rl-hairline" />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <RlTag tint="muted">{campaignLanguageLabel(campaign.language)}</RlTag>
          {latest ? <RlTag tint={sessionStatusTint(latest.status)}>{latest.status.replace(/_/g, " ")}</RlTag> : null}
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <span className="rl-card-meta truncate">{meta}</span>
          <span className="rl-open shrink-0">
            Open
            <RlIcon name="arrow" size={13} />
          </span>
        </div>
      </div>
    </RlCard>
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-7">
      <p className="sr-only">Loading campaigns…</p>
      <div className="rl-skeleton h-[84px]" aria-hidden="true" />
      <div className="grid gap-7 lg:grid-cols-3" aria-hidden="true">
        <div className="space-y-5 lg:col-span-2">
          <div className="rl-skeleton h-7 w-40" />
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rl-skeleton h-56" />
            <div className="rl-skeleton h-56" />
          </div>
        </div>
        <div className="space-y-6">
          <div className="rl-skeleton h-56" />
          <div className="rl-skeleton h-40" />
        </div>
      </div>
    </div>
  );
}

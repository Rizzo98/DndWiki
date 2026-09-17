// App chrome: a dark warm-black sidebar on desktop, collapsing to a top bar on
// narrow screens. Navigation is dark chrome; the content it frames is parchment
// (see .agents/skills/ravenlore-frontend-design).
//
// Inside a campaign the sidebar also carries that campaign's workspace: its
// tabs, and the wiki categories under "The world" — so the campaign is one
// place you are in, not a set of tabs on one page.

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { PAGE_KINDS, PAGE_KIND_LABELS, campaignsApi, type Campaign } from "@/lib/api";
import { PAGE_KIND_ICON, worldPath } from "@/lib/page-kinds";
import { useAsyncData } from "@/lib/use-async";
import { RlIcon, type RlIconName } from "@/components/ravenlore";

type NavLink = { href: string; label: string; icon: RlIconName; exact?: boolean };

const CAMPAIGNS_LINK: NavLink = { href: "/campaigns", label: "Campaigns", icon: "book" };
const ACCOUNT_LINKS: NavLink[] = [{ href: "/system", label: "System", icon: "gear" }];
/** The campaign id segment of a workspace URL ("/campaigns/<uuid>/…"). */
const CAMPAIGN_ID = /^\/campaigns\/([0-9a-fA-F-]{36})(?=\/|$)/;

export function Nav() {
  const { authenticated, logout, tokenParsed } = useAuth();
  const pathname = usePathname();
  const campaignId = CAMPAIGN_ID.exec(pathname)?.[1] ?? null;
  const { data: campaign } = useAsyncData<Campaign | null>(
    (t) => (campaignId ? campaignsApi.get(t, campaignId) : Promise.resolve(null)),
    [campaignId],
  );
  if (!authenticated) return null;

  const name = typeof tokenParsed?.name === "string" ? tokenParsed.name : null;
  const preferred = typeof tokenParsed?.preferred_username === "string" ? tokenParsed.preferred_username : null;
  const who = name ?? preferred ?? "Signed in";
  const initial = who.trim().charAt(0) || "?";
  const onProfile = pathname.startsWith("/profile");

  return (
    <>
      {/* Desktop: fixed sidebar; the account block and its utilities sit at the bottom. */}
      <aside className="rl-sidebar fixed inset-y-0 left-0 z-30 hidden lg:flex">
        <Link href="/" className="rl-sidebar-brand">
          <span className="text-xl">🎲</span> DnD Wiki
        </Link>

        <nav className="flex-1 space-y-1 overflow-y-auto p-3">
          <SidebarLink link={CAMPAIGNS_LINK} active={pathname === CAMPAIGNS_LINK.href} />
          {campaign ? <CampaignLinks campaign={campaign} pathname={pathname} /> : null}
        </nav>

        <div className="rl-sidebar-footer">
          {/* The account block IS the profile link — no separate Profile item. */}
          <Link
            href="/profile"
            className={"rl-user-link" + (onProfile ? " rl-user-link--active" : "")}
            aria-current={onProfile ? "page" : undefined}
          >
            <span className="rl-avatar">{initial}</span>
            <span className="min-w-0">
              <span className="rl-user-link-name block truncate">{who}</span>
              <span className="rl-user-link-hint block">View profile</span>
            </span>
          </Link>

          <div className="mt-2 space-y-1">
            {ACCOUNT_LINKS.map((l) => (
              <SidebarLink key={l.href} link={l} active={pathname.startsWith(l.href)} />
            ))}
            <button onClick={logout} className="rl-nav-item w-full">
              <RlIcon name="logout" size={17} />
              Sign out
            </button>
          </div>
        </div>
      </aside>

      {/* Narrow screens: the same navigation as a top bar (the campaign tabs
          live in the campaign layout's own strip). */}
      <header className="rl-nav sticky top-0 z-30 lg:hidden">
        <div className="flex items-center justify-between gap-3 px-4 py-3">
          <Link href="/" className="flex items-center gap-2 font-serif text-base font-bold">
            <span className="text-lg">🎲</span> DnD Wiki
          </Link>
          <nav className="flex items-center gap-1">
            {[CAMPAIGNS_LINK, ...ACCOUNT_LINKS].map((l) => (
              <Link
                key={l.href}
                href={l.href}
                aria-label={l.label}
                className={"rl-nav-item" + (pathname === l.href ? " rl-nav-item--active" : "")}
              >
                <RlIcon name={l.icon} size={18} />
              </Link>
            ))}
            <Link
              href="/profile"
              aria-label="Profile"
              className={"rl-nav-item" + (onProfile ? " rl-nav-item--active" : "")}
            >
              <span className="rl-avatar rl-avatar--sm">{initial}</span>
            </Link>
            <button onClick={logout} aria-label="Sign out" className="rl-nav-item">
              <RlIcon name="logout" size={18} />
            </button>
          </nav>
        </div>
      </header>
    </>
  );
}

/** The campaign workspace section of the sidebar. */
function CampaignLinks({ campaign, pathname }: { campaign: Campaign; pathname: string }) {
  const base = "/campaigns/" + campaign.id;
  const isDm = campaign.my_role === "dm";
  const tabs: NavLink[] = [
    { href: base, label: "Overview", icon: "home", exact: true },
    { href: base + "/sessions", label: "Sessions", icon: "mic" },
    { href: base + "/timeline", label: "Timeline", icon: "clock" },
    // The roster and the invite door are the DM's; players never see them.
    ...(isDm
      ? ([
          { href: base + "/members", label: "Members", icon: "users" },
          { href: base + "/invites", label: "Invites", icon: "key" },
        ] as NavLink[])
      : []),
  ];

  return (
    <>
      <div className="rl-sidebar-section">
        <Link href={base} className="rl-sidebar-section-title" title={campaign.name}>
          {campaign.name}
        </Link>
      </div>
      {tabs.map((t) => (
        <SidebarLink key={t.href} link={t} active={t.exact ? pathname === t.href : pathname.startsWith(t.href)} />
      ))}
      <span className="rl-nav-group">The world</span>
      {PAGE_KINDS.map((kind) => (
        <SidebarLink
          key={kind}
          link={{ href: worldPath(campaign.id, kind), label: PAGE_KIND_LABELS[kind], icon: PAGE_KIND_ICON[kind] }}
          active={pathname.startsWith(worldPath(campaign.id, kind))}
        />
      ))}
    </>
  );
}

function SidebarLink({ link, active }: { link: NavLink; active: boolean }) {
  return (
    <Link
      href={link.href}
      aria-current={active ? "page" : undefined}
      className={"rl-nav-item w-full" + (active ? " rl-nav-item--active" : "")}
    >
      <RlIcon name={link.icon} size={17} />
      {link.label}
    </Link>
  );
}

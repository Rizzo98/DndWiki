// Top navigation shell.

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth";

export function Nav() {
  const { authenticated, logout, tokenParsed } = useAuth();
  const pathname = usePathname();
  if (!authenticated) return null;

  const name = typeof tokenParsed?.name === "string" ? tokenParsed.name : null;
  const preferred = typeof tokenParsed?.preferred_username === "string" ? tokenParsed.preferred_username : null;

  const links = [
    { href: "/campaigns", label: "Campaigns" },
    { href: "/system", label: "System" },
    { href: "/profile", label: "Profile" },
  ];

  return (
    <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
        <Link href="/" className="flex items-center gap-2 text-lg font-bold text-slate-100">
          <span className="text-xl">🎲</span> DnD Wiki
        </Link>
        <nav className="flex items-center gap-1">
          {links.map((l) => {
            const active = pathname.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${
                  active ? "bg-slate-800 text-slate-100" : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {l.label}
              </Link>
            );
          })}
          <span className="mx-2 hidden text-sm text-slate-500 sm:inline">{name ?? preferred}</span>
          <button
            onClick={logout}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-slate-400 transition hover:bg-slate-800 hover:text-slate-200"
          >
            Sign out
          </button>
        </nav>
      </div>
    </header>
  );
}

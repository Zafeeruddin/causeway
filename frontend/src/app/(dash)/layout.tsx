"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useLive } from "@/lib/useLive";
import { administers, type Me } from "@/lib/types";
import { VERSION } from "@/lib/version";
import { Button, Dot, cx } from "@/components/ui";

/**
 * What each role is shown.
 *
 * Viewers are not given Overview and Connections and then stopped at the door:
 * the sections are absent. Most of the people using this are here to watch a
 * camera and take a clip away, and a menu full of gateways, jump hosts and
 * storage thresholds is not a permission problem for them, it is a confusion
 * one. The server enforces the same boundary either way.
 */
const NAV = [
  { href: "/", label: "Overview" },
  { href: "/profiles", label: "Connections" },
  { href: "/cameras", label: "Cameras" },
  { href: "/recordings", label: "Recordings" },
];

const VIEWER_NAV = [
  { href: "/cameras", label: "Cameras" },
  { href: "/recordings", label: "Recordings" },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch((err) => {
        if (err instanceof ApiError && err.isUnauthorized) router.replace("/login");
      })
      .finally(() => setChecked(true));
  }, [router]);

  // The socket lives at the shell so it survives navigation between pages.
  const { connected } = useLive(useCallback(() => {}, []));

  async function signOut() {
    await api.logout();
    router.replace("/login");
  }

  if (!checked) {
    return <div className="p-10 text-sm text-fg-3">Loading…</div>;
  }
  if (!me) return null;

  const nav = !administers(me.role)
    ? VIEWER_NAV
    : [...NAV, { href: "/admin", label: "Admin" }];

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-line bg-ink/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <Link href="/" className="shrink-0">
            <span className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">
              Camera tunnel
            </span>
            <span className="block text-sm font-semibold leading-tight">Control plane</span>
          </Link>

          <nav className="flex flex-1 items-center gap-1">
            {nav.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cx(
                    "rounded px-3 py-1.5 text-sm transition",
                    active ? "bg-ink-3 text-fg" : "text-fg-3 hover:text-fg",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="flex items-center gap-4">
            <span
              className="flex items-center gap-1.5 font-mono text-2xs uppercase tracking-wide text-fg-3"
              title={connected ? "Receiving live updates" : "Reconnecting to live updates"}
            >
              <Dot tone={connected ? "ok" : "warn"} />
              {connected ? "live" : "reconnecting"}
            </span>
            {/* Which build this is. Costs a badge and answers the first
                question of every support conversation. */}
            <span
              className="hidden font-mono text-2xs text-fg-3 lg:block"
              title="Deployed version"
            >
              v{VERSION}
            </span>
            <span className="hidden text-xs text-fg-3 sm:block">{me.email}</span>
            <Button size="sm" variant="quiet" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
    </div>
  );
}

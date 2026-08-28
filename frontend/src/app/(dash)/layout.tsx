"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { useLive } from "@/lib/useLive";
import type { Me } from "@/lib/types";
import { Button, Dot, cx } from "@/components/ui";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/profiles", label: "Connections" },
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

  const nav = me.role === "admin" ? [...NAV, { href: "/admin", label: "Admin" }] : NAV;

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

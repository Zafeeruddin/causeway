"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button, Card, Dot, Spinner } from "./ui";

/** How often a held screen asks again, so it lets people through on its own. */
const RECHECK_MS = 15_000;

type Gate =
  | { phase: "checking" }
  | { phase: "open" }
  | { phase: "maintenance"; note: string; rechecking: boolean }
  | { phase: "blocked"; endpoint: string; rechecking: boolean };

/**
 * Stands in front of a page until the deployment says it is usable.
 *
 * Two things hold it shut. Causeway records into object storage and cannot do
 * its job without it, so the first screen is the right place to hear that it is
 * down -- not the end of a recording, after someone has signed in, found a
 * camera and pressed Record. And a redeploy replaces the containers underneath
 * whoever is already signed in, which without this reads as requests failing
 * for no stated reason.
 *
 * It otherwise fails open. If the health request itself fails the page loads
 * anyway: the page's own requests will say what is wrong, and a check that
 * could not run is not grounds for locking everyone out. The exception is a
 * deploy already in progress -- there, the API going away is precisely what we
 * were told to expect, so once maintenance has been seen it stays until the
 * server itself says otherwise.
 */
export function ServiceGate({ children }: { children: React.ReactNode }) {
  const [gate, setGate] = useState<Gate>({ phase: "checking" });

  const check = useCallback(async () => {
    setGate((current) =>
      current.phase === "blocked" || current.phase === "maintenance"
        ? { ...current, rechecking: true }
        : current,
    );
    const health = await api.health().catch(() => undefined);
    setGate((current) => {
      if (health === undefined) {
        // Sticky through the seconds the api container is actually gone.
        return current.phase === "maintenance"
          ? { ...current, rechecking: false }
          : { phase: "open" };
      }
      if (health.status === "maintenance") {
        return {
          phase: "maintenance",
          note: health.maintenance?.note ?? "",
          rechecking: false,
        };
      }
      if (health.storage?.status === "unavailable") {
        return { phase: "blocked", endpoint: health.storage.endpoint, rechecking: false };
      }
      return { phase: "open" };
    });
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  const holding = gate.phase === "blocked" || gate.phase === "maintenance";
  useEffect(() => {
    if (!holding) return;
    const timer = window.setInterval(() => void check(), RECHECK_MS);
    return () => window.clearInterval(timer);
  }, [holding, check]);

  if (gate.phase === "checking") {
    return <div className="p-10 text-sm text-fg-3">Loading…</div>;
  }
  if (gate.phase === "open") return <>{children}</>;

  const maintenance = gate.phase === "maintenance";

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-7">
          <p className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">Causeway</p>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight">
            {maintenance ? "Under maintenance" : "Service unavailable"}
          </h1>
        </div>

        <Card className="p-6">
          <div className="flex flex-col gap-4">
            <p
              className={`flex items-center gap-2 font-mono text-2xs uppercase tracking-wide ${
                maintenance ? "text-warn" : "text-bad"
              }`}
            >
              <Dot tone={maintenance ? "warn" : "bad"} />
              {maintenance ? "Being updated" : "Recording storage unavailable"}
            </p>

            {maintenance ? (
              <>
                <p className="text-sm leading-relaxed text-fg">
                  Causeway is being updated right now. Recordings already finished are
                  safe, and the platform comes back on its own when the update is done.
                </p>
                {gate.note ? (
                  <div className="rounded border border-warn/40 bg-warn-wash px-3 py-2.5 text-xs leading-relaxed text-warn">
                    {gate.note}
                  </div>
                ) : null}
              </>
            ) : (
              <>
                <p className="text-sm leading-relaxed text-fg">
                  Causeway keeps every recording in object storage, and that storage is not
                  available right now. The platform can&apos;t be used until it is back.
                </p>
                <div className="rounded border border-bad/40 bg-bad-wash px-3 py-2.5 text-xs leading-relaxed text-bad">
                  Please contact your administrator and tell them that{" "}
                  <span className="font-mono font-medium">
                    {gate.endpoint || "its object storage"}
                  </span>{" "}
                  is unavailable.
                </div>
              </>
            )}

            <div className="mt-1 flex items-center justify-between gap-4">
              <p className="text-xs text-fg-3">
                Checking again every {RECHECK_MS / 1000} seconds.
              </p>
              <Button size="sm" onClick={() => void check()} disabled={gate.rechecking}>
                {gate.rechecking ? (
                  <span className="flex items-center gap-2">
                    <Spinner className="h-3 w-3" />
                    Checking
                  </span>
                ) : (
                  "Retry now"
                )}
              </Button>
            </div>
          </div>
        </Card>
      </div>
    </main>
  );
}

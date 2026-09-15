"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button, Card, Dot, Spinner } from "./ui";

/** How often a blocked screen asks again, so it lets people through on its own. */
const RECHECK_MS = 15_000;

type Gate =
  | { phase: "checking" }
  | { phase: "open" }
  | { phase: "blocked"; endpoint: string; rechecking: boolean };

/**
 * Stands in front of a page until the deployment's storage has answered.
 *
 * Causeway records into object storage and cannot do its job without it, so
 * the first screen is the right place to hear that it is down -- not the end of
 * a recording, after someone has signed in, found a camera and pressed Record.
 *
 * It only blocks on a definite answer. If the health request itself fails, the
 * page loads anyway: the page's own requests will say what is wrong, and a
 * check that could not run is not grounds for locking everyone out. A
 * deployment that has switched the check off reports "disabled" and is never
 * held here.
 */
export function ServiceGate({ children }: { children: React.ReactNode }) {
  const [gate, setGate] = useState<Gate>({ phase: "checking" });

  const check = useCallback(async () => {
    setGate((current) =>
      current.phase === "blocked" ? { ...current, rechecking: true } : current,
    );
    const storage = await api
      .health()
      .then((health) => health.storage)
      .catch(() => undefined);
    setGate(
      storage?.status === "unavailable"
        ? { phase: "blocked", endpoint: storage.endpoint, rechecking: false }
        : { phase: "open" },
    );
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  const blocked = gate.phase === "blocked";
  useEffect(() => {
    if (!blocked) return;
    const timer = window.setInterval(() => void check(), RECHECK_MS);
    return () => window.clearInterval(timer);
  }, [blocked, check]);

  if (gate.phase === "checking") {
    return <div className="p-10 text-sm text-fg-3">Loading…</div>;
  }
  if (gate.phase === "open") return <>{children}</>;

  const where = gate.endpoint || "its object storage";

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-7">
          <p className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">
            Causeway
          </p>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight">Service unavailable</h1>
        </div>

        <Card className="p-6">
          <div className="flex flex-col gap-4">
            <p className="flex items-center gap-2 font-mono text-2xs uppercase tracking-wide text-bad">
              <Dot tone="bad" />
              Recording storage unavailable
            </p>

            <p className="text-sm leading-relaxed text-fg">
              Causeway keeps every recording in object storage, and that storage is not
              available right now. The platform can&apos;t be used until it is back.
            </p>

            <div className="rounded border border-bad/40 bg-bad-wash px-3 py-2.5 text-xs leading-relaxed text-bad">
              Please contact your administrator and tell them that{" "}
              <span className="font-mono font-medium">{where}</span> is unavailable.
            </div>

            <div className="mt-1 flex items-center justify-between gap-4">
              <p className="text-xs text-fg-3">
                Checking again every {RECHECK_MS / 1000} seconds.
              </p>
              <Button
                size="sm"
                onClick={() => void check()}
                disabled={gate.rechecking}
              >
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

"use client";

import { bytes } from "@/lib/format";
import type { StorageUsage } from "@/lib/types";
import { Badge, cx, type Tone } from "./ui";

const STATE_TONE: Record<StorageUsage["state"], Tone> = {
  ok: "ok",
  warning: "warn",
  collecting: "warn",
  full: "bad",
};

const STATE_LABEL: Record<StorageUsage["state"], string> = {
  ok: "healthy",
  warning: "warning",
  collecting: "collecting",
  full: "full",
};

/**
 * Usage against the three thresholds.
 *
 * The thresholds are drawn on the bar because on Versity there is no bucket
 * quota behind them: these lines are the only ceiling, so they are worth
 * showing rather than leaving in a config file.
 */
export function StorageMeter({ usage }: { usage: StorageUsage }) {
  const scale = usage.hard_bytes || 1;
  const pct = (value: number) => Math.min(100, (value / scale) * 100);
  const tone = STATE_TONE[usage.state];
  const fill = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad" }[
    tone === "ok" ? "ok" : tone === "bad" ? "bad" : "warn"
  ];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span className="text-xl font-semibold tnum">{bytes(usage.used_bytes)}</span>
          <span className="text-xs text-fg-3">of {bytes(usage.hard_bytes)}</span>
        </div>
        <Badge tone={tone}>{STATE_LABEL[usage.state]}</Badge>
      </div>

      <div className="relative h-2 w-full overflow-hidden rounded-full bg-ink">
        <div
          className={cx("h-full rounded-full transition-all", fill)}
          style={{ width: `${pct(usage.used_bytes)}%` }}
        />
        {[
          { at: usage.warn_bytes, label: "warn" },
          { at: usage.gc_bytes, label: "collect" },
        ].map((mark) => (
          <span
            key={mark.label}
            title={`${mark.label} at ${bytes(mark.at)}`}
            className="absolute top-0 h-full w-px bg-fg-3/60"
            style={{ left: `${pct(mark.at)}%` }}
          />
        ))}
      </div>

      <div className="flex justify-between font-mono text-2xs text-fg-3 tnum">
        <span>warn {bytes(usage.warn_bytes)}</span>
        <span>collect {bytes(usage.gc_bytes)}</span>
        <span>refuse {bytes(usage.hard_bytes)}</span>
      </div>

      {usage.message ? <p className="text-xs leading-relaxed text-fg-3">{usage.message}</p> : null}
    </div>
  );
}

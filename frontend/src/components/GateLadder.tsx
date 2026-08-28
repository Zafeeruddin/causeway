"use client";

import type { Gate, GateStatus } from "@/lib/types";
import { Badge, Dot, Spinner, cx, type Tone } from "./ui";

/**
 * The eight gates, as the connection walks them.
 *
 * The point of showing skipped rungs rather than hiding them: "this profile
 * doesn't need a VPN" and "the VPN check never ran" look identical if you only
 * render what happened.
 */

const TONE: Record<GateStatus, Tone> = {
  passed: "ok",
  failed: "bad",
  blocked: "warn",
  skipped: "muted",
  running: "steel",
  pending: "muted",
};

const LABEL: Record<GateStatus, string> = {
  passed: "passed",
  failed: "failed",
  blocked: "needs you",
  skipped: "not needed",
  running: "checking",
  pending: "waiting",
};

/** Shown before an attempt has run, so the ladder has shape from the start. */
export const GATE_OUTLINE: { key: string; title: string }[] = [
  { key: "vpn_dial", title: "VPN dial" },
  { key: "cert_trust", title: "Certificate trust" },
  { key: "whitelist", title: "Whitelist check" },
  { key: "jump_route", title: "Route to the jump host" },
  { key: "ssh_auth", title: "SSH authentication" },
  { key: "port_forward", title: "Port forward" },
  { key: "camera_reachable", title: "Camera reachable" },
  { key: "stream_handshake", title: "Stream handshake" },
];

export function GateLadder({
  gates,
  running,
  onAction,
  compact = false,
}: {
  gates: Gate[];
  running?: boolean;
  onAction?: (gate: Gate) => void;
  compact?: boolean;
}) {
  const byKey = new Map(gates.map((g) => [g.key, g]));
  const rungs = GATE_OUTLINE.map(
    (outline, i) =>
      byKey.get(outline.key) ?? {
        key: outline.key,
        index: i + 1,
        title: outline.title,
        status: "pending" as GateStatus,
        message: "",
        detail: {},
        duration_ms: 0,
      },
  );

  return (
    <ol className="divide-y divide-line-soft">
      {rungs.map((gate) => {
        const tone = TONE[gate.status];
        const dim = gate.status === "skipped" || gate.status === "pending";
        return (
          <li key={gate.key} className={cx("flex gap-3 px-4 py-3", dim && "opacity-55")}>
            <span className="mt-0.5 w-5 shrink-0 font-mono text-2xs text-fg-3 tnum">
              {String(gate.index).padStart(2, "0")}
            </span>

            <span className="mt-1.5 shrink-0">
              {gate.status === "running" || (running && gate.status === "pending") ? (
                <Spinner className="text-steel" />
              ) : (
                <Dot tone={tone} />
              )}
            </span>

            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
                <span className={cx("text-sm", dim ? "text-fg-3" : "font-medium text-fg")}>
                  {gate.title}
                </span>
                <Badge tone={tone}>{LABEL[gate.status]}</Badge>
                {gate.duration_ms > 0 && !compact ? (
                  <span className="font-mono text-2xs text-fg-3 tnum">{gate.duration_ms} ms</span>
                ) : null}
              </div>

              {gate.message ? (
                <p
                  className={cx(
                    "mt-1 text-xs leading-relaxed",
                    gate.status === "failed" ? "text-bad" : "text-fg-3",
                  )}
                >
                  {gate.message}
                </p>
              ) : null}

              {gate.status === "blocked" && onAction ? (
                <button
                  onClick={() => onAction(gate)}
                  className="mt-2 rounded border border-warn/50 px-2.5 py-1 text-xs font-medium text-warn hover:bg-warn/10"
                >
                  Review and decide
                </button>
              ) : null}

              {gate.key === "stream_handshake" && gate.status === "passed" ? (
                <StreamReadout detail={gate.detail} />
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function StreamReadout({ detail }: { detail: Record<string, unknown> }) {
  const entries = [
    ["codec", detail.codec],
    ["resolution", detail.resolution],
    ["fps", detail.fps],
    ["audio", detail.audio_codec],
  ].filter(([, value]) => value) as [string, string | number][];

  if (!entries.length) return null;
  return (
    <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-baseline gap-1.5">
          <dt className="font-mono text-2xs uppercase tracking-wide text-fg-3">{key}</dt>
          <dd className="font-mono text-xs text-fg-2 tnum">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

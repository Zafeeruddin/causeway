"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ago, modeLabel } from "@/lib/format";
import { useLive } from "@/lib/useLive";
import type { CameraStats, LiveEvent, Profile, Recording, StorageUsage } from "@/lib/types";
import { StorageMeter } from "@/components/StorageMeter";
import { Badge, Card, CardHeader, Dot, Empty, Eyebrow, type Tone } from "@/components/ui";

/** Recordings listed under "Recent recordings". */
const RECENT = 4;

const PROFILE_TONE: Record<string, Tone> = {
  up: "ok",
  connecting: "steel",
  needs_interaction: "warn",
  degraded: "warn",
  failed: "bad",
  idle: "muted",
};

const PROFILE_LABEL: Record<string, string> = {
  up: "connected",
  connecting: "connecting",
  needs_interaction: "needs you",
  degraded: "degraded",
  failed: "failed",
  idle: "not connected",
};

export default function OverviewPage() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [cameras, setCameras] = useState<CameraStats | null>(null);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [storageHost, setStorageHost] = useState("");

  const load = useCallback(() => {
    api.profiles().then(setProfiles).catch(() => {});
    // Three numbers, counted by the server. Deriving them here meant fetching
    // every camera with its sources and profile.
    api.cameraStats().then(setCameras).catch(() => {});
    // Four rows are shown, so four are asked for. The plain list returns a
    // hundred.
    api.recordingPage("", [], 1, RECENT).then((p) => setRecordings(p.items)).catch(() => {});
    api.storage().then(setUsage).catch(() => {});
    api.health().then((h) => setStorageHost(h.storage?.endpoint ?? "")).catch(() => {});
  }, []);

  useEffect(load, [load]);

  useLive(
    useCallback((event: LiveEvent) => {
      if (event.type !== "profile_state") return;
      const { profile_id, state, detail, tunnel_ip } = event.payload ?? {};
      setProfiles((current) =>
        current.map((p) =>
          p.id === profile_id
            ? { ...p, state, state_detail: detail ?? "", tunnel_ip: tunnel_ip ?? null }
            : p,
        ),
      );
    }, []),
  );

  const online = profiles.filter((p) => p.state === "up").length;
  const needsAttention = profiles.filter(
    (p) => p.state === "needs_interaction" || p.state === "failed",
  );

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Eyebrow>Overview</Eyebrow>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {online > 0
            ? `${online} of ${profiles.length} connections up`
            : profiles.length
              ? "Nothing connected"
              : "Nothing set up yet"}
        </h1>
      </div>

      {needsAttention.length ? (
        <Card className="border-warn/40 bg-warn-wash/40">
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-warn">
              {needsAttention.length === 1
                ? "One connection needs attention"
                : `${needsAttention.length} connections need attention`}
            </p>
            <ul className="mt-2 flex flex-col gap-1">
              {needsAttention.map((p) => (
                <li key={p.id} className="text-xs text-fg-2">
                  <Link href="/profiles" className="font-medium underline underline-offset-2">
                    {p.name}
                  </Link>{" "}
                  &mdash; {p.state_detail || PROFILE_LABEL[p.state]}
                </li>
              ))}
            </ul>
          </div>
        </Card>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Connections"
            sub="How each team reaches its cameras"
            action={
              <Link href="/profiles" className="text-xs text-steel hover:underline">
                Manage
              </Link>
            }
          />
          {profiles.length === 0 ? (
            <Empty
              title="No connection profiles yet"
              hint="A profile declares the path to a camera: a VPN hop, a jump host, both, or neither."
            />
          ) : (
            <ul className="divide-y divide-line-soft">
              {profiles.map((profile) => (
                <li key={profile.id} className="flex items-center gap-4 px-5 py-3.5">
                  <Dot tone={PROFILE_TONE[profile.state] ?? "muted"} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{profile.name}</p>
                    <p className="truncate text-xs text-fg-3">
                      {modeLabel(profile.mode)}
                      {profile.tunnel_ip ? ` · ${profile.tunnel_ip}` : ""}
                    </p>
                  </div>
                  <Badge tone={PROFILE_TONE[profile.state] ?? "muted"}>
                    {PROFILE_LABEL[profile.state] ?? profile.state}
                  </Badge>
                  <span className="hidden w-20 text-right font-mono text-2xs text-fg-3 sm:block">
                    {ago(profile.last_connected_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader
              title="Storage"
              sub={storageHost ? `Object storage · ${storageHost}` : "Object storage"}
            />
            <div className="px-5 py-4">
              {usage ? (
                <StorageMeter usage={usage} />
              ) : (
                <p className="text-xs text-fg-3">Loading…</p>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader title="Cameras" />
            <div className="grid grid-cols-2 divide-x divide-line-soft">
              <Stat label="cameras" value={cameras ? cameras.cameras : "—"} />
              <Stat
                label="sources reachable"
                value={cameras ? `${cameras.sources_reachable}/${cameras.sources}` : "—"}
              />
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Recent recordings"
              action={
                <Link href="/recordings" className="text-xs text-steel hover:underline">
                  All
                </Link>
              }
            />
            {recordings.length === 0 ? (
              <p className="px-5 py-6 text-xs text-fg-3">Nothing recorded yet.</p>
            ) : (
              <ul className="divide-y divide-line-soft">
                {recordings.map((r) => (
                  <li key={r.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                    <span className="min-w-0 truncate text-xs text-fg-2">
                      {r.camera_name || r.id.slice(0, 8)}
                    </span>
                    <Badge tone={r.state === "complete" ? "ok" : r.state === "failed" ? "bad" : "steel"}>
                      {r.state}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="px-5 py-4">
      <p className="text-xl font-semibold tnum">{value}</p>
      <p className="mt-0.5 font-mono text-2xs uppercase tracking-wide text-fg-3">{label}</p>
    </div>
  );
}

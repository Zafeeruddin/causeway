"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { ago, bytes, modeLabel } from "@/lib/format";
import type { Camera, Gate, ImportResult, Me, Profile, Source } from "@/lib/types";
import { GateLadder } from "@/components/GateLadder";
import { LivePreview } from "@/components/LivePreview";
import {
  Badge, Banner, Button, Card, CardHeader, Dot, Empty, Eyebrow, Field,
  Input, Modal, PasswordInput, Select, Textarea, type Tone,
} from "@/components/ui";

type AddMode = "single" | "paste" | "csv";

export default function CamerasPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [adding, setAdding] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [gates, setGates] = useState<Record<string, Gate[]>>({});
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [recording, setRecording] = useState(false);
  const [watching, setWatching] = useState<Camera | null>(null);

  const load = useCallback(async () => {
    setCameras(await api.cameras());
  }, []);

  useEffect(() => {
    api.me().then(setMe).catch(() => {});
    api.profiles().then(setProfiles).catch(() => {});
    load().catch(() => {});
  }, [load]);

  async function test(camera: Camera) {
    setTesting(camera.id);
    setError("");
    try {
      const results = await api.testCamera(camera.id);
      setGates((current) => ({ ...current, [camera.id]: results }));
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The test could not run.");
    } finally {
      setTesting(null);
    }
  }

  function toggle(id: string) {
    setSelection((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <Eyebrow>Cameras</Eyebrow>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">
            {cameras.length} camera{cameras.length === 1 ? "" : "s"}
          </h1>
        </div>
        <div className="flex gap-2">
          <Button
            disabled={selection.size === 0}
            onClick={() => setRecording(true)}
          >
            Record {selection.size ? `${selection.size} selected` : ""}
          </Button>
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add cameras
          </Button>
        </div>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      {cameras.length === 0 ? (
        <Card>
          <Empty
            title="No cameras yet"
            hint="Add one at a time, paste a block of RTSP URLs, or upload a CSV exported from your NVR."
            action={
              <Button variant="primary" onClick={() => setAdding(true)}>
                Add cameras
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {cameras.map((camera) => (
            <CameraRow
              key={camera.id}
              camera={camera}
              profile={profiles.find((p) => p.id === camera.profile_id)}
              gates={gates[camera.id] ?? []}
              testing={testing === camera.id}
              selected={selection.has(camera.id)}
              onToggle={() => toggle(camera.id)}
              onPreview={() => setWatching(camera)}
              onTest={() => test(camera)}
              onDelete={async () => {
                await api.deleteCamera(camera.id);
                await load();
              }}
            />
          ))}
        </div>
      )}

      <AddCamerasModal
        open={adding}
        teams={me?.teams ?? []}
        profiles={profiles}
        onClose={() => setAdding(false)}
        onDone={async () => {
          setAdding(false);
          await load();
        }}
      />

      {watching ? (
        <Modal
          open
          onClose={() => setWatching(null)}
          title={watching.name}
          sub="Live from the camera. Closing this stops your view of it."
          width="max-w-3xl"
        >
          <LivePreview camera={watching} />
        </Modal>
      ) : null}

      <RecordModal
        open={recording}
        cameraIds={[...selection]}
        cameras={cameras}
        onClose={() => setRecording(false)}
        onStarted={() => {
          setRecording(false);
          setSelection(new Set());
        }}
      />
    </div>
  );
}

function CameraRow({
  camera, profile, gates, testing, selected, onToggle, onPreview, onTest, onDelete,
}: {
  camera: Camera;
  profile?: Profile;
  gates: Gate[];
  testing: boolean;
  selected: boolean;
  onToggle: () => void;
  onPreview: () => void;
  onTest: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const health = cameraTone(camera.sources);

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-4 px-5 py-4">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          className="h-4 w-4 accent-[#79b7d8]"
          aria-label={`Select ${camera.name}`}
        />
        <Dot tone={health} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{camera.name}</p>
          <p className="truncate text-xs text-fg-3">
            {camera.location ? `${camera.location} · ` : ""}
            {profile ? `${profile.name} (${modeLabel(profile.mode)})` : "no profile"}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {camera.sources.map((source) => (
            <SourceChip key={source.id} source={source} />
          ))}
        </div>

        <div className="flex gap-2">
          <Button size="sm" onClick={onPreview} disabled={!camera.sources.length}>
            Preview
          </Button>
          <Button size="sm" onClick={onTest} disabled={testing}>
            {testing ? "Testing…" : "Test"}
          </Button>
          <Button size="sm" variant="quiet" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide" : "Details"}
          </Button>
          <Button size="sm" variant="danger" onClick={onDelete}>
            Remove
          </Button>
        </div>
      </div>

      {open ? (
        <div className="border-t border-line-soft">
          <div className="grid gap-px bg-line-soft sm:grid-cols-2">
            {camera.sources.map((source) => (
              <div key={source.id} className="bg-ink-2 px-5 py-4">
                <div className="flex items-center gap-2">
                  <Badge tone={source.kind === "rtsp" ? "steel" : "zone"}>{source.kind}</Badge>
                  {source.uses_profile_path ? (
                    <span className="font-mono text-2xs text-fg-3">via the profile path</span>
                  ) : (
                    <span className="font-mono text-2xs text-fg-3">reached directly</span>
                  )}
                </div>
                <p className="mt-2 break-all font-mono text-xs text-fg-2">{source.url}</p>
                <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1">
                  {source.codec ? <Micro label="codec" value={source.codec} /> : null}
                  {source.width ? (
                    <Micro label="size" value={`${source.width}×${source.height}`} />
                  ) : null}
                  {source.fps ? <Micro label="fps" value={String(source.fps)} /> : null}
                  <Micro label="last probe" value={ago(source.last_probe_at)} />
                </dl>
                {source.last_probe_ok === false ? (
                  <p className="mt-2 text-xs text-bad">{source.last_probe_detail}</p>
                ) : null}
              </div>
            ))}
          </div>

          {gates.length ? (
            <div className="border-t border-line-soft">
              <p className="px-5 pt-4 font-mono text-2xs uppercase tracking-[0.14em] text-fg-3">
                Last test
              </p>
              <GateLadder gates={gates} compact />
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function SourceChip({ source }: { source: Source }) {
  const tone: Tone =
    source.last_probe_ok === true ? "ok" : source.last_probe_ok === false ? "bad" : "muted";
  return (
    <Badge tone={tone}>
      {source.kind}
      {source.codec ? ` · ${source.codec}` : ""}
    </Badge>
  );
}

function Micro({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="font-mono text-2xs uppercase tracking-wide text-fg-3">{label}</dt>
      <dd className="font-mono text-xs text-fg-2 tnum">{value}</dd>
    </div>
  );
}

function cameraTone(sources: Source[]): Tone {
  if (!sources.length) return "muted";
  if (sources.some((s) => s.last_probe_ok === false)) return "bad";
  if (sources.every((s) => s.last_probe_ok === true)) return "ok";
  return "muted";
}

/* ---- adding ---- */

/**
 * Credentials an NVR baked into the URL it exported.
 *
 * The server strips these out and seals them whether or not we look, so
 * reading them here changes nothing about what is stored -- it changes whether
 * the person can see that it happened. Percent-decoded, because that is the
 * form the camera is actually sent: a password typed as `CTC2.5++` is exported
 * as `CTC2.5%2B%2B`, and showing the encoded form invites someone to "correct"
 * it into a password that has never existed.
 */
function credentialsInUrl(url: string): { username: string; password: string } | null {
  const authority = /^[a-z][a-z0-9+.-]*:\/\/([^/@\s]+)@/i.exec(url.trim());
  if (!authority) return null;
  const [user, ...rest] = authority[1]!.split(":");
  if (!user) return null;
  return { username: decode(user), password: decode(rest.join(":")) };
}

function decode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    // A stray % that is not an escape. Better the literal text than nothing.
    return value;
  }
}

function AddCamerasModal({
  open, teams, profiles, onClose, onDone,
}: {
  open: boolean;
  teams: { id: string; name: string }[];
  profiles: Profile[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [mode, setMode] = useState<AddMode>("single");
  const [teamId, setTeamId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ImportResult | null>(null);

  const [name, setName] = useState("");
  const [rtsp, setRtsp] = useState("");
  const [hls, setHls] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);

  const teamProfiles = useMemo(
    () => profiles.filter((p) => !teamId || p.team_id === teamId),
    [profiles, teamId],
  );

  // Derived rather than written into the fields: a URL edited back to a plain
  // one has to give the person their own typing back, not the NVR's.
  const fromUrl = useMemo(() => credentialsInUrl(rtsp), [rtsp]);
  const shownUsername = fromUrl ? fromUrl.username : username;
  const shownPassword = fromUrl ? fromUrl.password : password;

  useEffect(() => {
    if (teams[0] && !teamId) setTeamId(teams[0]!.id);
  }, [teams, teamId]);
  useEffect(() => {
    if (teamProfiles[0] && !teamProfiles.some((p) => p.id === profileId)) {
      setProfileId(teamProfiles[0]!.id);
    }
  }, [teamProfiles, profileId]);

  async function run(dryRun: boolean) {
    setBusy(true);
    setError("");
    try {
      if (mode === "single") {
        await api.createCamera({
          team_id: teamId,
          profile_id: profileId,
          name,
          sources: [
            ...(rtsp
              ? [
                  {
                    kind: "rtsp" as const,
                    url: rtsp,
                    username: shownUsername,
                    password: shownPassword,
                  },
                ]
              : []),
            ...(hls ? [{ kind: "hls" as const, url: hls }] : []),
          ],
        });
        onDone();
        return;
      }
      const result =
        mode === "paste"
          ? await api.importPasted({ team_id: teamId, profile_id: profileId, text, dry_run: dryRun })
          : file
            ? await api.importCsv(teamId, profileId, file, dryRun)
            : null;

      if (!result) {
        setError("Choose a CSV file first.");
        return;
      }
      setPreview(result);
      if (!dryRun) onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Add cameras" width="max-w-2xl">
      <div className="flex flex-col gap-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Team">
            <Select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </Select>
          </Field>
          <Field label="Connection profile" hint="How these cameras are reached.">
            <Select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              {teamProfiles.length === 0 ? <option value="">No profiles in this team</option> : null}
              {teamProfiles.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </Field>
        </div>

        <div className="flex gap-1 rounded border border-line bg-ink p-1">
          {(
            [
              ["single", "One camera"],
              ["paste", "Paste a list"],
              ["csv", "Upload CSV"],
            ] as [AddMode, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              onClick={() => {
                setMode(value);
                setPreview(null);
              }}
              className={`flex-1 rounded px-3 py-1.5 text-xs transition ${
                mode === value ? "bg-ink-3 text-fg" : "text-fg-3 hover:text-fg"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {mode === "single" ? (
          <div className="flex flex-col gap-4">
            <Field label="Name">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Gate camera" />
            </Field>
            <Field label="RTSP URL">
              <Input
                value={rtsp}
                onChange={(e) => setRtsp(e.target.value)}
                placeholder="rtsp://10.20.30.42:554/Streaming/Channels/101"
                className="font-mono text-xs"
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label="Username"
                hint={
                  fromUrl
                    ? "Read out of the URL above. Edit the URL to change it."
                    : "Optional if the URL already carries one."
                }
              >
                <Input
                  value={shownUsername}
                  onChange={(e) => setUsername(e.target.value)}
                  readOnly={!!fromUrl}
                  className={fromUrl ? "cursor-not-allowed text-fg-2" : undefined}
                  autoComplete="off"
                />
              </Field>
              <Field
                label="Password"
                hint={
                  fromUrl
                    ? "Read out of the URL above. Stripped out of it and sealed on save."
                    : "Sealed on save; never shown again."
                }
              >
                <PasswordInput
                  value={shownPassword}
                  onChange={(e) => setPassword(e.target.value)}
                  readOnly={!!fromUrl}
                  className={fromUrl ? "cursor-not-allowed text-fg-2" : undefined}
                  autoComplete="new-password"
                />
              </Field>
            </div>
            <Field
              label="HLS playlist"
              hint="Optional. The inferred feed QA compares against the raw camera."
            >
              <Input
                value={hls}
                onChange={(e) => setHls(e.target.value)}
                placeholder="https://cdn.example.com/live/gate/index.m3u8"
                className="font-mono text-xs"
              />
            </Field>
          </div>
        ) : mode === "paste" ? (
          <Field
            label="One stream per line"
            hint="A leading name is honoured: “Gate camera, rtsp://…”. Blank lines and # comments are ignored."
          >
            <Textarea
              rows={8}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"Gate, rtsp://10.20.30.41:554/Streaming/Channels/101\nrtsp://10.20.30.42:554/Streaming/Channels/101"}
            />
          </Field>
        ) : (
          <Field
            label="CSV file"
            hint="Needs a header row with an rtsp_url or hls_url column. name, location, username and password are picked up if present."
          >
            <input
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="w-full rounded border border-line bg-ink px-3 py-2 text-xs text-fg-2 file:mr-3 file:rounded file:border-0 file:bg-ink-3 file:px-3 file:py-1.5 file:text-xs file:text-fg-2"
            />
          </Field>
        )}

        {preview ? <ImportPreviewPanel result={preview} /> : null}
        {error ? <Banner tone="bad" title={error} /> : null}

        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          {mode !== "single" ? (
            <Button onClick={() => run(true)} disabled={busy}>
              Preview
            </Button>
          ) : null}
          <Button variant="primary" onClick={() => run(false)} disabled={busy || !profileId}>
            {busy ? "Working…" : mode === "single" ? "Add camera" : "Import"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function ImportPreviewPanel({ result }: { result: ImportResult }) {
  return (
    <div className="rounded border border-line bg-ink">
      <div className="flex items-center justify-between border-b border-line-soft px-4 py-2.5">
        <span className="text-xs font-medium">{result.summary}</span>
        {result.dry_run ? <Badge tone="steel">preview</Badge> : <Badge tone="ok">imported</Badge>}
      </div>
      <div className="max-h-56 overflow-y-auto">
        {result.cameras.map((camera, i) => (
          <div key={i} className="flex items-center gap-3 border-b border-line-soft px-4 py-2">
            <span className="w-40 shrink-0 truncate text-xs text-fg-2">{camera.name}</span>
            <span className="truncate font-mono text-2xs text-fg-3">
              {camera.sources.map((s) => s.url).join("  ")}
            </span>
          </div>
        ))}
        {[...result.duplicates.map((d) => ["duplicate", d] as const),
          ...result.rejected.map((r) => ["rejected", r] as const)].map(([kind, issue], i) => (
          <div key={`${kind}-${i}`} className="flex items-center gap-3 border-b border-line-soft px-4 py-2">
            <Badge tone={kind === "duplicate" ? "warn" : "bad"}>{kind}</Badge>
            <span className="truncate font-mono text-2xs text-fg-3">
              {issue.line ? `line ${issue.line}: ` : ""}
              {issue.value}
            </span>
            <span className="ml-auto shrink-0 text-2xs text-fg-3">{issue.reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---- recording ---- */

function RecordModal({
  open, cameraIds, cameras, onClose, onStarted,
}: {
  open: boolean;
  cameraIds: string[];
  cameras: Camera[];
  onClose: () => void;
  onStarted: () => void;
}) {
  const [seconds, setSeconds] = useState(300);
  const [estimate, setEstimate] = useState<{ allowed: boolean; reason: string; estimated_bytes: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // The refusal has to be visible before anyone presses record, not after.
  useEffect(() => {
    if (!open || cameraIds.length === 0) return;
    let cancelled = false;
    api
      .estimate(cameraIds, seconds)
      .then((result) => {
        if (!cancelled) setEstimate(result);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open, cameraIds, seconds]);

  const chosen = cameras.filter((c) => cameraIds.includes(c.id));

  async function start() {
    setBusy(true);
    setError("");
    try {
      await api.startRecording(cameraIds, seconds);
      onStarted();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start recording.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Record ${chosen.length} camera${chosen.length === 1 ? "" : "s"}`}
      sub="Every source on each camera is recorded, RTSP and HLS alike."
    >
      <div className="flex flex-col gap-5">
        <Field label={`Duration — ${Math.round(seconds / 60)} minutes`}>
          <input
            type="range"
            min={60}
            max={900}
            step={60}
            value={seconds}
            onChange={(e) => setSeconds(Number(e.target.value))}
            className="w-full accent-[#79b7d8]"
          />
          <div className="flex justify-between font-mono text-2xs text-fg-3">
            <span>1 min</span>
            <span>15 min max</span>
          </div>
        </Field>

        {estimate ? (
          <Banner
            tone={estimate.allowed ? "steel" : "bad"}
            title={
              estimate.allowed
                ? `About ${bytes(estimate.estimated_bytes)} of storage`
                : "Not enough room"
            }
          >
            {estimate.allowed ? null : estimate.reason}
          </Banner>
        ) : null}

        <ul className="flex flex-col gap-1">
          {chosen.map((camera) => (
            <li key={camera.id} className="flex items-center gap-2 text-xs text-fg-2">
              <Dot tone="steel" />
              {camera.name}
              <span className="text-fg-3">
                {camera.sources.map((s) => s.kind).join(" + ")}
              </span>
            </li>
          ))}
        </ul>

        {error ? <Banner tone="bad" title={error} /> : null}

        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            onClick={start}
            disabled={busy || estimate?.allowed === false}
          >
            {busy ? "Starting…" : "Start recording"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

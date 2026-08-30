"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type ProfileInput } from "@/lib/api";
import { MODE_LABEL, MODE_HINT, ago, modeHint, modeLabel, vpnLabel } from "@/lib/format";
import { useLive } from "@/lib/useLive";
import type { Gate, LiveEvent, Me, Profile, ReachMode, VpnKind } from "@/lib/types";
import { CertificateDialog } from "@/components/CertificateDialog";
import { GateLadder } from "@/components/GateLadder";
import {
  Badge, Banner, Button, Card, CardHeader, Empty, Eyebrow, Field, Input,
  Modal, PasswordInput, Select, Textarea, type Tone,
} from "@/components/ui";

const STATE_TONE: Record<string, Tone> = {
  up: "ok", connecting: "steel", needs_interaction: "warn",
  degraded: "warn", failed: "bad", idle: "muted",
};
const STATE_LABEL: Record<string, string> = {
  up: "connected", connecting: "connecting", needs_interaction: "needs you",
  degraded: "degraded", failed: "failed", idle: "not connected",
};

interface CertPrompt {
  profileId: string; host: string; fingerprint: string; algorithm: string; reason: string;
}

export default function ProfilesPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [gates, setGates] = useState<Record<string, Gate[]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [cert, setCert] = useState<CertPrompt | null>(null);
  const [removing, setRemoving] = useState<Profile | null>(null);

  const load = useCallback(async () => {
    const list = await api.profiles();
    setProfiles(list);
    setSelected((current) => current ?? list[0]?.id ?? null);
  }, []);

  useEffect(() => {
    api.me().then(setMe).catch(() => {});
    load().catch(() => {});
  }, [load]);

  useEffect(() => {
    if (!selected) return;
    api.gates(selected).then((g) => setGates((c) => ({ ...c, [selected]: g }))).catch(() => {});
  }, [selected]);

  // Gates arrive one at a time as the ladder walks, so the panel fills in live
  // rather than sitting on a spinner until the whole attempt finishes.
  useLive(
    useCallback((event: LiveEvent) => {
      if (event.type === "gate") {
        const gate = event.payload as unknown as Gate & { profile_id: string };
        setGates((current) => {
          const existing = current[gate.profile_id] ?? [];
          const next = existing.filter((g) => g.key !== gate.key);
          next.push(gate);
          next.sort((a, b) => a.index - b.index);
          return { ...current, [gate.profile_id]: next };
        });
      }
      if (event.type === "profile_state") {
        const { profile_id, state, detail, tunnel_ip } = event.payload ?? {};
        setProfiles((current) =>
          current.map((p) =>
            p.id === profile_id
              ? { ...p, state, state_detail: detail ?? "", tunnel_ip: tunnel_ip ?? null }
              : p,
          ),
        );
      }
    }, []),
  );

  async function connect(profile: Profile) {
    setBusy(profile.id);
    setError("");
    setGates((current) => ({ ...current, [profile.id]: [] }));
    try {
      const result = await api.connect(profile.id);
      setGates((current) => ({ ...current, [profile.id]: result.gates }));
      const action = result.action_required;
      if (action?.action === "accept_certificate") {
        setCert({
          profileId: profile.id,
          host: String(action.host ?? profile.vpn_gateway),
          fingerprint: String(action.fingerprint ?? ""),
          algorithm: String(action.algorithm ?? "sha256"),
          reason: String(action.reason ?? ""),
        });
      }
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not connect.");
    } finally {
      setBusy(null);
    }
  }

  async function acceptCertificate(fingerprint: string) {
    if (!cert) return;
    setBusy(cert.profileId);
    try {
      const result = await api.trust(cert.profileId, fingerprint);
      setGates((current) => ({ ...current, [cert.profileId]: result.gates }));
      setCert(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not pin the certificate.");
    } finally {
      setBusy(null);
    }
  }

  async function remove(profile: Profile) {
    setBusy(profile.id);
    setError("");
    try {
      await api.deleteProfile(profile.id);
      setRemoving(null);
      // The selection pointed at a row that no longer exists; letting load()
      // choose again is what stops the panel rendering a deleted profile.
      setSelected(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove the profile.");
      setRemoving(null);
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(profile: Profile) {
    setBusy(profile.id);
    try {
      await api.disconnect(profile.id);
      await load();
    } finally {
      setBusy(null);
    }
  }

  const current = profiles.find((p) => p.id === selected) ?? null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <Eyebrow>Connections</Eyebrow>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Connection profiles</h1>
          <p className="mt-1.5 max-w-2xl text-sm text-fg-3">
            A profile declares the path to a set of cameras. Hops it does not use are skipped,
            visibly, so &ldquo;not needed&rdquo; never looks like &ldquo;never checked&rdquo;.
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreating(true)}>
          New profile
        </Button>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      {removing ? (
        <Modal
          open
          onClose={() => setRemoving(null)}
          title={`Remove ${removing.name}?`}
          sub="The tunnel is brought down first. Cameras that use it must be moved or removed beforehand."
        >
          <div className="flex flex-col gap-4">
            <p className="text-sm text-fg-2">
              {modeLabel(removing.mode)}
              {removing.vpn_gateway ? ` · ${removing.vpn_gateway}` : ""}
              {removing.jump_host ? ` · ${removing.jump_host}` : ""}
            </p>
            <p className="text-xs text-fg-3">
              Its stored VPN and SSH credentials go with it and cannot be recovered.
            </p>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setRemoving(null)}>Keep it</Button>
              <Button variant="danger" onClick={() => remove(removing)}>
                Remove profile
              </Button>
            </div>
          </div>
        </Modal>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,320px)_1fr]">
        <Card className="self-start">
          <CardHeader title="Profiles" sub={`${profiles.length} configured`} />
          {profiles.length === 0 ? (
            <Empty title="No profiles yet" hint="Create one to describe how to reach your cameras." />
          ) : (
            <ul className="divide-y divide-line-soft">
              {profiles.map((profile) => (
                <li key={profile.id}>
                  <button
                    onClick={() => setSelected(profile.id)}
                    className={`w-full px-5 py-3.5 text-left transition ${
                      profile.id === selected ? "bg-ink-3" : "hover:bg-ink-3/50"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium">{profile.name}</span>
                      <Badge tone={STATE_TONE[profile.state] ?? "muted"}>
                        {STATE_LABEL[profile.state] ?? profile.state}
                      </Badge>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-fg-3">{modeLabel(profile.mode)}</p>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {current ? (
          <div className="flex flex-col gap-6">
            <Card>
              <CardHeader
                title={current.name}
                sub={modeHint(current.mode)}
                action={
                  <div className="flex gap-2">
                    {current.state === "up" ? (
                      <Button size="sm" onClick={() => disconnect(current)} disabled={busy === current.id}>
                        Disconnect
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => connect(current)}
                      disabled={busy === current.id}
                    >
                      {busy === current.id ? "Connecting…" : "Connect"}
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => setRemoving(current)}
                      disabled={busy === current.id}
                    >
                      Remove
                    </Button>
                  </div>
                }
              />
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 px-5 py-4 sm:grid-cols-3">
                <Detail label="mode" value={modeLabel(current.mode)} />
                <Detail label="vpn" value={vpnLabel(current.vpn_kind)} />
                {current.vpn_gateway ? <Detail label="gateway" value={current.vpn_gateway} mono /> : null}
                {current.jump_host ? (
                  <Detail label="jump host" value={`${current.jump_username}@${current.jump_host}`} mono />
                ) : null}
                {current.tunnel_ip ? <Detail label="tunnel ip" value={current.tunnel_ip} mono /> : null}
                <Detail label="last connected" value={ago(current.last_connected_at)} />
                {current.trusted_cert ? (
                  <Detail
                    label="certificate"
                    value={`pinned ${current.trusted_cert.slice(0, 12)}…`}
                    mono
                  />
                ) : null}
              </dl>
            </Card>

            <Card>
              <CardHeader
                title="Gates"
                sub="Every connection walks these in order. Skipped rungs are shown, not hidden."
              />
              <GateLadder
                gates={gates[current.id] ?? []}
                running={busy === current.id}
                onAction={(gate) => {
                  if (gate.detail?.action === "accept_certificate") {
                    setCert({
                      profileId: current.id,
                      host: String(gate.detail.host ?? current.vpn_gateway),
                      fingerprint: String(gate.detail.fingerprint ?? ""),
                      algorithm: String(gate.detail.algorithm ?? "sha256"),
                      reason: String(gate.detail.reason ?? ""),
                    });
                  }
                }}
              />
            </Card>
          </div>
        ) : null}
      </div>

      {cert ? (
        <CertificateDialog
          open
          onClose={() => setCert(null)}
          onAccept={acceptCertificate}
          host={cert.host}
          fingerprint={cert.fingerprint}
          algorithm={cert.algorithm}
          reason={cert.reason}
          busy={busy === cert.profileId}
        />
      ) : null}

      <NewProfileModal
        open={creating}
        teams={me?.teams ?? []}
        onClose={() => setCreating(false)}
        onCreated={async () => {
          setCreating(false);
          await load();
        }}
      />
    </div>
  );
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="font-mono text-2xs uppercase tracking-wide text-fg-3">{label}</dt>
      <dd className={`mt-0.5 truncate text-sm ${mono ? "font-mono text-xs text-fg-2" : "text-fg-2"}`}>
        {value}
      </dd>
    </div>
  );
}

const MODES: ReachMode[] = ["direct", "vpn_only", "jump_only", "vpn_jump"];
const VPN_KINDS: VpnKind[] = ["fortinet", "globalprotect", "wireguard"];

function NewProfileModal({
  open, teams, onClose, onCreated,
}: {
  open: boolean;
  teams: { id: string; name: string }[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [form, setForm] = useState<ProfileInput>({
    team_id: "", name: "", mode: "vpn_jump", vpn_kind: "fortinet",
    vpn_gateway: "", vpn_port: 443, vpn_username: "", vpn_password: "",
    jump_host: "", jump_port: 22, jump_username: "", jump_auth: "password",
    jump_password: "", jump_private_key: "",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (teams[0] && !form.team_id) setForm((f) => ({ ...f, team_id: teams[0]!.id }));
  }, [teams, form.team_id]);

  const set = <K extends keyof ProfileInput>(key: K, value: ProfileInput[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const mode = form.mode;
  const needsVpn = mode === "vpn_only" || mode === "vpn_jump";
  const needsJump = mode === "jump_only" || mode === "vpn_jump";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      // Only send the half of the form the chosen mode actually uses.
      const payload: ProfileInput = {
        team_id: form.team_id,
        name: form.name,
        mode: form.mode,
        vpn_kind: needsVpn ? form.vpn_kind : "none",
        ...(needsVpn
          ? {
              vpn_gateway: form.vpn_gateway, vpn_port: form.vpn_port,
              vpn_username: form.vpn_username, vpn_password: form.vpn_password,
              wg_config: form.wg_config,
            }
          : {}),
        ...(needsJump
          ? {
              jump_host: form.jump_host, jump_port: form.jump_port,
              jump_username: form.jump_username, jump_auth: form.jump_auth,
              jump_password: form.jump_auth === "password" ? form.jump_password : "",
              jump_private_key: form.jump_auth === "key" ? form.jump_private_key : "",
            }
          : {}),
        whitelist_url: form.whitelist_url || null,
      };
      await api.createProfile(payload);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the profile.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New connection profile" width="max-w-2xl">
      <form onSubmit={submit} className="flex flex-col gap-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Team">
            <Select value={form.team_id} onChange={(e) => set("team_id", e.target.value)} required>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>{team.name}</option>
              ))}
            </Select>
          </Field>
          <Field label="Name">
            <Input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="Camera VPN"
              required
            />
          </Field>
        </div>

        <Field label="How do we reach these cameras?" hint={modeHint(mode)}>
          <Select value={mode} onChange={(e) => set("mode", e.target.value as ReachMode)}>
            {MODES.map((m) => (
              <option key={m} value={m}>{modeLabel(m)}</option>
            ))}
          </Select>
        </Field>

        {needsVpn ? (
          <fieldset className="rounded border border-line-soft p-4">
            <legend className="px-1.5 font-mono text-2xs uppercase tracking-[0.14em] text-steel">
              VPN hop
            </legend>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Type">
                <Select
                  value={form.vpn_kind}
                  onChange={(e) => set("vpn_kind", e.target.value as VpnKind)}
                >
                  {VPN_KINDS.map((k) => (
                    <option key={k} value={k}>{vpnLabel(k)}</option>
                  ))}
                </Select>
              </Field>
              {form.vpn_kind === "wireguard" ? null : (
                <Field label="Gateway">
                  <Input
                    value={form.vpn_gateway}
                    onChange={(e) => set("vpn_gateway", e.target.value)}
                    placeholder="vpn.example.com"
                    required
                  />
                </Field>
              )}
            </div>

            {form.vpn_kind === "wireguard" ? (
              <div className="mt-4">
                <Field label="Interface configuration" hint="The contents of your wg .conf file.">
                  <Textarea
                    rows={6}
                    value={form.wg_config ?? ""}
                    onChange={(e) => set("wg_config", e.target.value)}
                    placeholder="[Interface]&#10;PrivateKey = …"
                  />
                </Field>
              </div>
            ) : (
              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                <Field label="Username">
                  <Input
                    value={form.vpn_username}
                    onChange={(e) => set("vpn_username", e.target.value)}
                    autoComplete="off"
                  />
                </Field>
                <Field label="Password" hint="Sealed on save and never shown again.">
                  <PasswordInput
                    value={form.vpn_password}
                    onChange={(e) => set("vpn_password", e.target.value)}
                    autoComplete="new-password"
                  />
                </Field>
              </div>
            )}
          </fieldset>
        ) : null}

        {needsJump ? (
          <fieldset className="rounded border border-line-soft p-4">
            <legend className="px-1.5 font-mono text-2xs uppercase tracking-[0.14em] text-steel">
              Jump host
            </legend>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Address">
                <Input
                  value={form.jump_host}
                  onChange={(e) => set("jump_host", e.target.value)}
                  placeholder="10.20.30.71"
                  required
                />
              </Field>
              <Field label="Username">
                <Input value={form.jump_username} onChange={(e) => set("jump_username", e.target.value)} />
              </Field>
              <Field label="Authentication">
                <Select
                  value={form.jump_auth}
                  onChange={(e) => set("jump_auth", e.target.value as "password" | "key")}
                >
                  <option value="password">Password</option>
                  <option value="key">SSH key</option>
                </Select>
              </Field>
            </div>
            <div className="mt-4">
              {form.jump_auth === "key" ? (
                <Field label="Private key">
                  <Textarea
                    rows={5}
                    value={form.jump_private_key}
                    onChange={(e) => set("jump_private_key", e.target.value)}
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  />
                </Field>
              ) : (
                <Field label="Password" hint="Sealed on save and never shown again.">
                  <PasswordInput
                    value={form.jump_password}
                    onChange={(e) => set("jump_password", e.target.value)}
                    autoComplete="new-password"
                  />
                </Field>
              )}
            </div>
          </fieldset>
        ) : null}

        <Field
          label="Whitelist check URL"
          hint="Optional. Fetched from inside the tunnel to confirm this account is allowed through."
        >
          <Input
            value={form.whitelist_url ?? ""}
            onChange={(e) => set("whitelist_url", e.target.value)}
            placeholder="https://internal.example.com/allow-check"
          />
        </Field>

        {error ? <Banner tone="bad" title={error} /> : null}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create profile"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

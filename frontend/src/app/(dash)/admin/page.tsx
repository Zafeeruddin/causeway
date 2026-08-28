"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { ago } from "@/lib/format";
import type { Team, User } from "@/lib/types";
import {
  Badge, Banner, Button, Card, CardHeader, Empty, Eyebrow, Field, Input, Modal, Select,
} from "@/components/ui";

export default function AdminPage() {
  const [teams, setTeams] = useState<Team[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState("");
  const [creatingTeam, setCreatingTeam] = useState(false);
  const [creatingUser, setCreatingUser] = useState(false);
  const [assigning, setAssigning] = useState<Team | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, u] = await Promise.all([api.teams(), api.users()]);
      setTeams(t);
      setUsers(u);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Eyebrow>Admin</Eyebrow>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Teams and people</h1>
        <p className="mt-1.5 max-w-2xl text-sm text-fg-3">
          A team owns its cameras, its recordings and its connection profile. Members see
          the union of their teams and nothing else.
        </p>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Teams"
            action={
              <Button size="sm" onClick={() => setCreatingTeam(true)}>
                New team
              </Button>
            }
          />
          {teams.length === 0 ? (
            <Empty title="No teams yet" hint="Create one before adding cameras." />
          ) : (
            <ul className="divide-y divide-line-soft">
              {teams.map((team) => (
                <li key={team.id} className="flex items-center gap-3 px-5 py-3.5">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{team.name}</p>
                    <p className="font-mono text-2xs text-fg-3">{team.slug}</p>
                  </div>
                  <Badge>{team.member_count ?? 0} members</Badge>
                  <Button size="sm" variant="quiet" onClick={() => setAssigning(team)}>
                    Manage
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader
            title="People"
            action={
              <Button size="sm" onClick={() => setCreatingUser(true)}>
                New account
              </Button>
            }
          />
          <ul className="divide-y divide-line-soft">
            {users.map((user) => (
              <li key={user.id} className="flex items-center gap-3 px-5 py-3.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">{user.display_name || user.email}</p>
                  <p className="truncate font-mono text-2xs text-fg-3">{user.email}</p>
                </div>
                <Badge tone={user.role === "admin" ? "steel" : "muted"}>{user.role}</Badge>
                <span className="w-20 text-right text-2xs text-fg-3">
                  {ago(user.last_login_at)}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <NewTeamModal
        open={creatingTeam}
        onClose={() => setCreatingTeam(false)}
        onDone={async () => {
          setCreatingTeam(false);
          await load();
        }}
      />
      <NewUserModal
        open={creatingUser}
        onClose={() => setCreatingUser(false)}
        onDone={async () => {
          setCreatingUser(false);
          await load();
        }}
      />
      <MembersModal
        team={assigning}
        users={users}
        onClose={() => setAssigning(null)}
        onChanged={load}
      />
    </div>
  );
}

function NewTeamModal({
  open, onClose, onDone,
}: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createTeam(name, slug);
      setName("");
      setSlug("");
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the team.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New team">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Name">
          <Input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              // Suggest a slug, but let it be overridden.
              setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""));
            }}
            placeholder="ACME"
            required
          />
        </Field>
        <Field label="Slug" hint="Used as the storage prefix for this team's recordings.">
          <Input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            pattern="[a-z0-9][a-z0-9-]*"
            className="font-mono text-xs"
            required
          />
        </Field>
        {error ? <Banner tone="bad" title={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create team"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function NewUserModal({
  open, onClose, onDone,
}: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("member");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createUser({ email, password, role });
      setEmail("");
      setPassword("");
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the account.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="New account">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Email">
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password" hint="At least 10 characters.">
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={10}
            autoComplete="new-password"
            required
          />
        </Field>
        <Field label="Role" hint="Admins see every team and manage accounts.">
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </Select>
        </Field>
        {error ? <Banner tone="bad" title={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create account"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function MembersModal({
  team, users, onClose, onChanged,
}: {
  team: Team | null;
  users: User[];
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  if (!team) return null;

  async function add(userId: string) {
    setBusy(userId);
    try {
      await api.addMember(team!.id, userId);
      await onChanged();
    } finally {
      setBusy(null);
    }
  }

  async function remove(userId: string) {
    setBusy(userId);
    try {
      await api.removeMember(team!.id, userId);
      await onChanged();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal open onClose={onClose} title={`${team.name} members`} sub={team.slug}>
      <ul className="flex flex-col gap-1.5">
        {users.map((user) => (
          <li
            key={user.id}
            className="flex items-center gap-3 rounded border border-line bg-ink px-4 py-2.5"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm">{user.display_name || user.email}</p>
              <p className="truncate font-mono text-2xs text-fg-3">{user.email}</p>
            </div>
            {user.role === "admin" ? (
              <span className="text-2xs text-fg-3">sees every team</span>
            ) : (
              <div className="flex gap-1.5">
                <Button size="sm" disabled={busy === user.id} onClick={() => add(user.id)}>
                  Add
                </Button>
                <Button
                  size="sm"
                  variant="quiet"
                  disabled={busy === user.id}
                  onClick={() => remove(user.id)}
                >
                  Remove
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Modal>
  );
}

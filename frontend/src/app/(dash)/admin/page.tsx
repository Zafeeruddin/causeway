"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { ago } from "@/lib/format";
import type { Me, Team, User } from "@/lib/types";
import {
  Badge, Banner, Button, Card, CardHeader, Empty, Eyebrow, Field, Input, Modal, Select,
} from "@/components/ui";

export default function AdminPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState("");
  const [creatingTeam, setCreatingTeam] = useState(false);
  const [creatingUser, setCreatingUser] = useState(false);
  const [assigning, setAssigning] = useState<Team | null>(null);

  const load = useCallback(async () => {
    try {
      const [who, t, u] = await Promise.all([api.me(), api.teams(), api.users()]);
      setMe(who);
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
          A team owns its cameras, its recordings and its connection profiles. Everyone sees
          the union of their teams and nothing else; what they may do inside them is their role.
        </p>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Teams"
            action={
              // A team is the boundary every other permission is drawn against,
              // so only the account that owns the deployment draws one.
              me?.role === "superadmin" ? (
                <Button size="sm" onClick={() => setCreatingTeam(true)}>
                  New team
                </Button>
              ) : null
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
                <RolePicker
                  user={user}
                  me={me}
                  onChanged={load}
                  onError={setError}
                />
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
        me={me}
        teams={teams}
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
            placeholder="MOFA"
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

/**
 * The role, editable in place.
 *
 * A role is the only thing about an account that routinely turns out wrong --
 * someone joins as a viewer and starts running the cameras, or the reverse --
 * and the alternative to changing it here is deleting the account and making
 * another, which loses everything attached to it.
 *
 * The server decides all of this again; what the picker does is avoid offering
 * a choice that will come back as a 403.
 */
function RolePicker({
  user, me, onChanged, onError,
}: {
  user: User;
  me: Me | null;
  onChanged: () => Promise<void> | void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const superadmin = me?.role === "superadmin";
  const self = me?.id === user.id;
  // Nobody edits their own role: the last superadmin demoting themselves locks
  // the deployment out of its own administration.
  const locked = self || (!superadmin && user.role === "superadmin");

  if (locked) {
    return (
      <Badge tone={user.role === "viewer" ? "muted" : "steel"}>
        {user.role}
        {self ? " · you" : ""}
      </Badge>
    );
  }

  return (
    <Select
      aria-label={`Role for ${user.email}`}
      className="w-36 py-1 text-xs"
      value={user.role}
      disabled={busy}
      onChange={async (event) => {
        setBusy(true);
        try {
          await api.updateUser(user.id, { role: event.target.value });
          await onChanged();
        } catch (err) {
          onError(err instanceof ApiError ? err.message : "Could not change the role.");
        } finally {
          setBusy(false);
        }
      }}
    >
      <option value="viewer">viewer</option>
      <option value="admin">admin</option>
      {superadmin ? <option value="superadmin">superadmin</option> : null}
    </Select>
  );
}

function NewUserModal({
  open, me, teams, onClose, onDone,
}: {
  open: boolean;
  me: Me | null;
  teams: Team[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [teamId, setTeamId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A superadmin may make any role and place it anywhere. An admin may make
  // viewers, in their own teams -- the server enforces both, and offering the
  // rest here would only be a 403 waiting to happen.
  const superadmin = me?.role === "superadmin";

  useEffect(() => {
    if (teams[0] && !teamId) setTeamId(teams[0].id);
  }, [teams, teamId]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createUser({
        email,
        password,
        role,
        team_ids: teamId ? [teamId] : [],
      });
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
        <Field
          label="Role"
          hint={
            superadmin
              ? "Superadmins own the deployment. Admins own their own teams. Viewers watch cameras."
              : "You can create viewers: they watch, record and download, and change nothing."
          }
        >
          <Select value={role} onChange={(e) => setRole(e.target.value)} disabled={!superadmin}>
            <option value="viewer">Viewer</option>
            {superadmin ? <option value="admin">Admin</option> : null}
            {superadmin ? <option value="superadmin">Superadmin</option> : null}
          </Select>
        </Field>
        <Field
          label="Team"
          hint={
            superadmin
              ? "Optional for a superadmin, who sees every team regardless."
              : "The team this account will be able to see."
          }
        >
          <Select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            {superadmin ? <option value="">No team yet</option> : null}
            {teams.map((team) => (
              <option key={team.id} value={team.id}>{team.name}</option>
            ))}
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
            {user.role === "superadmin" ? (
              // Membership is meaningless for an account that already sees all
              // of it, so offering to add or remove them would be a lie.
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

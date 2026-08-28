/**
 * Typed fetch wrapper.
 *
 * Everything goes through the same origin (see the rewrite in next.config.ts),
 * so the httpOnly session cookie travels with each request and no token is
 * ever held in JavaScript.
 */

import type {
  Admission, Camera, ConnectResponse, DownloadLink, Gate, ImportResult, Me,
  Profile, Recording, ReachMode, SourceKind, SshAuth, StorageUsage, Team, User, VpnKind,
} from "./types";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
  /** 401 means the session went away, which the shell handles by signing out. */
  get isUnauthorized() { return this.status === 401; }
  get isStorageFull() { return this.status === 507; }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      ...(init.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    throw new ApiError(response.status, await readError(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list; surface the first usefully.
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0];
      const field = Array.isArray(first?.loc) ? first.loc[first.loc.length - 1] : "";
      return field ? `${field}: ${first.msg}` : String(first.msg ?? "Invalid request.");
    }
  } catch {
    /* fall through to the status text */
  }
  return response.statusText || "Something went wrong.";
}

const json = (body: unknown) => ({ body: JSON.stringify(body) });

export interface ProfileInput {
  team_id: string; name: string; mode: ReachMode;
  vpn_kind?: VpnKind; vpn_gateway?: string; vpn_port?: number;
  vpn_username?: string; vpn_password?: string; vpn_realm?: string; wg_config?: string;
  jump_host?: string; jump_port?: number; jump_username?: string;
  jump_auth?: SshAuth; jump_password?: string; jump_private_key?: string;
  whitelist_url?: string | null;
}

export interface CameraInput {
  team_id: string; profile_id: string; name: string; location?: string;
  sources: { kind: SourceKind; url: string; username?: string; password?: string }[];
}

export const api = {
  // auth
  login: (email: string, password: string) =>
    request<Me>("/api/auth/login", { method: "POST", ...json({ email, password }) }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<Me>("/api/auth/me"),

  // profiles
  profiles: () => request<Profile[]>("/api/profiles"),
  createProfile: (body: ProfileInput) =>
    request<Profile>("/api/profiles", { method: "POST", ...json(body) }),
  deleteProfile: (id: string) => request<void>(`/api/profiles/${id}`, { method: "DELETE" }),
  connect: (id: string) =>
    request<ConnectResponse>(`/api/profiles/${id}/connect`, { method: "POST" }),
  trust: (id: string, fingerprint: string) =>
    request<ConnectResponse>(`/api/profiles/${id}/trust`, { method: "POST", ...json({ fingerprint }) }),
  disconnect: (id: string) =>
    request<Profile>(`/api/profiles/${id}/disconnect`, { method: "POST" }),
  gates: (id: string) => request<Gate[]>(`/api/profiles/${id}/gates`),

  // cameras
  cameras: () => request<Camera[]>("/api/cameras"),
  createCamera: (body: CameraInput) =>
    request<Camera>("/api/cameras", { method: "POST", ...json(body) }),
  deleteCamera: (id: string) => request<void>(`/api/cameras/${id}`, { method: "DELETE" }),
  testCamera: (id: string) => request<Gate[]>(`/api/cameras/${id}/test`, { method: "POST" }),
  importPasted: (body: { team_id: string; profile_id: string; text: string; dry_run: boolean }) =>
    request<ImportResult>("/api/cameras/import", { method: "POST", ...json(body) }),
  importCsv: (teamId: string, profileId: string, file: File, dryRun: boolean) => {
    const form = new FormData();
    form.append("file", file);
    const query = new URLSearchParams({
      team_id: teamId, profile_id: profileId, dry_run: String(dryRun),
    });
    return request<ImportResult>(`/api/cameras/import/csv?${query}`, { method: "POST", body: form });
  },

  // recordings
  recordings: () => request<Recording[]>("/api/recordings"),
  startRecording: (camera_ids: string[], seconds: number) =>
    request<Recording[]>("/api/recordings", { method: "POST", ...json({ camera_ids, seconds }) }),
  estimate: (camera_ids: string[], seconds: number) =>
    request<Admission>("/api/recordings/estimate", { method: "POST", ...json({ camera_ids, seconds }) }),
  downloads: (id: string) => request<DownloadLink[]>(`/api/recordings/${id}/downloads`),

  // storage + admin
  storage: () => request<StorageUsage>("/api/storage/usage"),
  teams: () => request<Team[]>("/api/admin/teams"),
  createTeam: (name: string, slug: string, description = "") =>
    request<Team>("/api/admin/teams", { method: "POST", ...json({ name, slug, description }) }),
  users: () => request<User[]>("/api/admin/users"),
  createUser: (body: { email: string; password: string; display_name?: string; role?: string }) =>
    request<User>("/api/admin/users", { method: "POST", ...json(body) }),
  addMember: (teamId: string, userId: string) =>
    request<void>(`/api/admin/teams/${teamId}/members`, { method: "POST", ...json({ user_id: userId }) }),
  removeMember: (teamId: string, userId: string) =>
    request<void>(`/api/admin/teams/${teamId}/members/${userId}`, { method: "DELETE" }),
};

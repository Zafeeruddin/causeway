/** Mirrors app/api/schemas.py. Kept hand-written and small rather than generated. */

export type Role = "superadmin" | "admin" | "viewer";

/** Administers something somewhere. Which team is the server's question. */
export const administers = (role: Role) => role === "superadmin" || role === "admin";

export type ReachMode = "direct" | "vpn_only" | "jump_only" | "vpn_jump";
export type VpnKind = "none" | "fortinet" | "globalprotect" | "wireguard";
export type SshAuth = "password" | "key";
export type SourceKind = "rtsp" | "hls";
export type ProfileState =
  | "idle" | "connecting" | "needs_interaction" | "up" | "degraded" | "failed";
export type GateStatus =
  | "pending" | "running" | "passed" | "skipped" | "failed" | "blocked";
export type RecordingState =
  | "queued" | "recording" | "recovering" | "finalizing" | "complete" | "failed" | "cancelled";

export interface Team { id: string; name: string; slug: string; description?: string; member_count?: number }
export interface Me { id: string; email: string; display_name: string; role: Role; teams: Team[] }

export interface Profile {
  id: string; team_id: string; name: string; mode: ReachMode;
  state: ProfileState; state_detail: string;
  vpn_kind: VpnKind; vpn_gateway: string; vpn_port: number; vpn_username: string;
  has_vpn_password: boolean;
  jump_host: string; jump_port: number; jump_username: string; jump_auth: SshAuth;
  has_jump_credentials: boolean;
  whitelist_url: string | null;
  trusted_cert: string | null; trusted_cert_algorithm: string; trusted_cert_accepted_at: string | null;
  tunnel_ip: string | null; last_connected_at: string | null;
}

export interface Gate {
  key: string; index: number; title: string; status: GateStatus;
  message: string; detail: Record<string, unknown>; duration_ms: number;
}

export interface ConnectResponse {
  attempt_id: string; state: ProfileState; gates: Gate[];
  action_required: Record<string, unknown> | null;
}

export interface Source {
  id: string; kind: SourceKind; url: string; host: string; port: number; username: string;
  uses_profile_path: boolean;
  last_probe_at: string | null; last_probe_ok: boolean | null; last_probe_detail: string;
  codec: string | null; width: number | null; height: number | null; fps: number | null;
}

export interface Camera {
  id: string; team_id: string; profile_id: string; name: string;
  /** How this camera is reached, carried here so a viewer -- who is not allowed
   *  near the profiles API -- still gets a label on the row. */
  profile_name: string; profile_mode: ReachMode | null;
  location: string; is_enabled: boolean; sources: Source[];
}

/** A live WebRTC view of one camera, held open only while somebody is watching. */
export interface Preview {
  id: string; camera_id: string; source_kind: SourceKind;
  /** The MediaMTX path. Random, so knowing a camera id is not enough to watch it. */
  path: string;
  /** Where the browser negotiates WebRTC. Everything after that is browser to MediaMTX. */
  whep_url: string;
  started_at: string;
  /** The hard stop. The stream also ends on its own once the last viewer leaves. */
  expires_at: string;
  viewers: number;
  /** What the camera is sending, as MediaMTX names it: "H264", "H265". */
  codec: string;
  /** This view's claim on the stream. Hand it back when closing. */
  viewer: string;
}

export interface ImportIssue { line: number; value: string; reason: string }
export interface ImportResult {
  summary: string; created: number; dry_run: boolean;
  cameras: { name: string; location: string; sources: { kind: SourceKind; url: string }[] }[];
  duplicates: ImportIssue[]; rejected: ImportIssue[];
}

export interface Recording {
  id: string; team_id: string; camera_id: string; state: RecordingState;
  requested_seconds: number; started_at: string | null; finished_at: string | null;
  captured_seconds: number; gap_seconds: number; total_bytes: number; failure_reason: string;
}

/** A stretch of video with no discontinuity, placed on both clocks. */
export interface Span { media_start: number; wall_start: number; seconds: number }

export interface GapMark { wall_start: number; seconds: number; cause: string; detail: string }

export interface Track {
  source_kind: SourceKind;
  url: string; expires_in: number; bytes: number;
  captured_seconds: number; gap_seconds: number; starts_at: number;
  spans: Span[]; gaps: GapMark[];
}

export interface Alignment { method: "wall_clock"; accuracy_seconds: number; note: string }

export interface Comparison {
  recording_id: string; camera_id: string; camera_name: string;
  state: RecordingState; requested_seconds: number;
  /** Everything else in here is seconds from this moment. */
  origin: string;
  window_seconds: number;
  tracks: Track[];
  alignment: Alignment;
}

export interface DownloadLink {
  /** null for gaps.json, which describes the session rather than one source. */
  source_kind: SourceKind | null; filename: string; bytes: number; url: string; expires_in: number;
}

export interface StorageUsage {
  used_bytes: number; warn_bytes: number; gc_bytes: number; hard_bytes: number;
  state: "ok" | "warning" | "collecting" | "full";
  message: string; by_team: Record<string, number>;
}

export interface Admission {
  allowed: boolean; reason: string; estimated_bytes: number; headroom_bytes: number;
}

export interface User {
  id: string; email: string; display_name: string; role: Role;
  is_active: boolean; last_login_at: string | null;
}

/** Envelope pushed over the WebSocket. */
export interface LiveEvent {
  type: "gate" | "profile_state" | "recording" | "ready" | "ping";
  team_id?: string;
  at?: string;
  payload?: Record<string, any>;
  teams?: string[];
}

export function bytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const scaled = value / 1024 ** i;
  return `${scaled >= 100 || i === 0 ? Math.round(scaled) : scaled.toFixed(1)} ${units[i]}`;
}

export function duration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

export function ago(iso: string | null): string {
  if (!iso) return "never";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** Group a fingerprint so it can be compared against a dialog by eye. */
export function fingerprint(value: string, groups = 2): string {
  return (value.match(new RegExp(`.{1,${groups}}`, "g")) ?? []).join(":");
}

/**
 * Lookups fall back to the raw value rather than rendering "undefined": the
 * backend can grow a mode or a VPN kind before the dashboard knows about it,
 * and showing the identifier beats showing nothing.
 */
export function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key.replace(/_/g, " ");
}

export const MODE_LABEL: Record<string, string> = {
  direct: "Direct",
  vpn_only: "VPN only",
  jump_only: "Jump host only",
  vpn_jump: "VPN + jump host",
};

export const MODE_HINT: Record<string, string> = {
  direct: "This host already sits on the camera network.",
  vpn_only: "The VPN routes to cameras with no jump host in between.",
  jump_only: "Already on the network; cameras sit behind a jump host.",
  vpn_jump: "Off-network, cameras behind a jump host.",
};

export const VPN_LABEL: Record<string, string> = {
  none: "None",
  fortinet: "FortiClient / Fortinet SSL-VPN",
  globalprotect: "GlobalProtect / Palo Alto",
  wireguard: "WireGuard",
};

export const modeLabel = (mode: string) => label(MODE_LABEL, mode);
export const modeHint = (mode: string) => MODE_HINT[mode] ?? "";
export const vpnLabel = (kind: string) => label(VPN_LABEL, kind);

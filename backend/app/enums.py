"""Vocabulary shared across the whole system.

These names show up in the database, on the wire, and in the UI, so they change
together or not at all.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class VpnKind(StrEnum):
    """Which VPN client dials this profile. `NONE` is a real driver, not a null."""

    NONE = "none"
    FORTINET = "fortinet"
    GLOBALPROTECT = "globalprotect"
    WIREGUARD = "wireguard"


class ReachMode(StrEnum):
    """How a connection profile reaches its cameras.

    Derived from whether the profile carries a VPN hop, a jump hop, or both --
    stored explicitly so the gate ladder can skip rungs without re-deriving it.
    """

    DIRECT = "direct"
    VPN_ONLY = "vpn_only"
    JUMP_ONLY = "jump_only"
    VPN_JUMP = "vpn_jump"

    @property
    def has_vpn(self) -> bool:
        return self in (ReachMode.VPN_ONLY, ReachMode.VPN_JUMP)

    @property
    def has_jump(self) -> bool:
        return self in (ReachMode.JUMP_ONLY, ReachMode.VPN_JUMP)


class SshAuth(StrEnum):
    PASSWORD = "password"
    KEY = "key"


class ProfileState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    #: Dial stopped and is waiting on a human -- certificate trust today, MFA later.
    NEEDS_INTERACTION = "needs_interaction"
    UP = "up"
    DEGRADED = "degraded"
    FAILED = "failed"


class SourceKind(StrEnum):
    RTSP = "rtsp"
    HLS = "hls"


class GateStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    #: Not applicable to this profile's reachability mode.
    SKIPPED = "skipped"
    FAILED = "failed"
    #: Waiting on the user (accept a certificate, answer a challenge).
    BLOCKED = "blocked"


class RecordingState(StrEnum):
    QUEUED = "queued"
    RECORDING = "recording"
    #: Stream is down; the supervisor is redialling and a gap is open.
    RECOVERING = "recovering"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

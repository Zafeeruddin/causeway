"""Vocabulary shared across the whole system.

These names show up in the database, on the wire, and in the UI, so they change
together or not at all.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """What a person may do, and to how much of the deployment.

    Three tiers, because the people using this are not one kind of person. Most
    of them want to watch a camera and take a clip away; a few of them own the
    plumbing that makes that possible; one of them owns the deployment. Showing
    the plumbing to the first group is not a security failure, it is a usability
    one -- so the tiers govern what is *visible* as much as what is permitted.
    """

    #: The deployment. Every team, every profile, every user, no exceptions.
    SUPERADMIN = "superadmin"
    #: Their own teams, completely: profiles, cameras, and who is in the team.
    ADMIN = "admin"
    #: Their own teams' cameras, and only the parts of that a person watching a
    #: camera needs: preview, record, download. No profiles, no plumbing.
    VIEWER = "viewer"
    #: A viewer with the writing taken away. Sees the teams it is in and can
    #: watch a camera live; cannot record, cannot delete, cannot change its own
    #: password. It exists for one situation -- an account whose credentials are
    #: published, on an instance reachable from the internet -- and the password
    #: rule is the point of it: a shared login the first visitor can change is a
    #: login you have given away.
    DEMO = "demo"

    @property
    def may_administer(self) -> bool:
        """Whether this role manages anything at all, in any team."""
        return self in (Role.SUPERADMIN, Role.ADMIN)

    @property
    def may_write(self) -> bool:
        """Whether this role may change anything at all.

        Separate from :attr:`may_administer` because they answer different
        questions. A viewer administers nothing and still writes -- starts a
        recording, deletes one, changes its own password. Demo is the only tier
        where the answer is no, and every mutating route asks this rather than
        listing roles for itself, so a new route is safe by construction.
        """
        return self is not Role.DEMO


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

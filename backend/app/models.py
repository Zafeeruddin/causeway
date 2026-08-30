"""Schema.

Two rules run through all of it. Everything a team owns carries ``team_id``,
including the connection profile -- scoping cameras but not the profile they are
reached through would let one team borrow another's tunnel. And no column ever
holds a credential: they hold opaque refs that :mod:`app.security.secrets`
resolves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import (
    GateStatus,
    ProfileState,
    ReachMode,
    RecordingState,
    Role,
    SourceKind,
    SshAuth,
    VpnKind,
)

#: JSONB on Postgres, plain JSON everywhere else -- so the suite can run against
#: SQLite without a second schema definition.
JsonColumn = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---- identity ----------------------------------------------------------


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[Role] = mapped_column(String(16), default=Role.VIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[TeamMember]] = relationship(back_populates="user")

    @property
    def is_superadmin(self) -> bool:
        return Role(self.role) is Role.SUPERADMIN

    @property
    def may_administer(self) -> bool:
        """Administers their own teams. Says nothing about which teams those are."""
        return Role(self.role).may_administer


class Team(Base, TimestampMixin):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    #: Per-team share of the storage cap, in bytes. Null means the global cap.
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)

    members: Mapped[list[TeamMember]] = relationship(back_populates="team")


class TeamMember(Base, TimestampMixin):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


# ---- reaching cameras --------------------------------------------------


class ConnectionProfile(Base, TimestampMixin):
    """How a team reaches its cameras: optional VPN hop, optional jump hop."""

    __tablename__ = "connection_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[ReachMode] = mapped_column(String(16), nullable=False)

    vpn_kind: Mapped[VpnKind] = mapped_column(String(24), default=VpnKind.NONE, nullable=False)
    vpn_gateway: Mapped[str] = mapped_column(String(255), default="")
    vpn_port: Mapped[int] = mapped_column(Integer, default=443)
    vpn_username: Mapped[str] = mapped_column(String(255), default="")
    vpn_password_ref: Mapped[str | None] = mapped_column(Text)
    vpn_realm: Mapped[str] = mapped_column(String(120), default="")
    vpn_config_ref: Mapped[str | None] = mapped_column(Text)  # WireGuard interface config

    #: Pinned by the user answering gate 2. Cleared to force the prompt again.
    trusted_cert: Mapped[str | None] = mapped_column(String(160))
    trusted_cert_algorithm: Mapped[str] = mapped_column(String(16), default="sha256")
    trusted_cert_accepted_by: Mapped[str | None] = mapped_column(String(36))
    trusted_cert_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jump_host: Mapped[str] = mapped_column(String(255), default="")
    jump_port: Mapped[int] = mapped_column(Integer, default=22)
    jump_username: Mapped[str] = mapped_column(String(120), default="")
    jump_auth: Mapped[SshAuth] = mapped_column(String(16), default=SshAuth.PASSWORD)
    jump_password_ref: Mapped[str | None] = mapped_column(Text)
    jump_key_ref: Mapped[str | None] = mapped_column(Text)

    whitelist_url: Mapped[str | None] = mapped_column(String(500))

    state: Mapped[ProfileState] = mapped_column(String(24), default=ProfileState.IDLE)
    state_detail: Mapped[str] = mapped_column(Text, default="")
    tunnel_ip: Mapped[str | None] = mapped_column(String(64))
    namespace: Mapped[str | None] = mapped_column(String(64))
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cameras: Mapped[list[Camera]] = relationship(back_populates="profile")

    @property
    def reach_mode(self) -> ReachMode:
        return ReachMode(self.mode)


class GateRun(Base):
    """One rung of one connection attempt. The audit trail for 'it was working
    yesterday' conversations."""

    __tablename__ = "gate_runs"
    __table_args__ = (Index("ix_gate_runs_profile_time", "profile_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("connection_profiles.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str | None] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    gate_key: Mapped[str] = mapped_column(String(48), nullable=False)
    gate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[GateStatus] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Tunnel(Base, TimestampMixin):
    """A live port lease. Persisted so a restart does not collide with itself."""

    __tablename__ = "tunnels"
    __table_args__ = (UniqueConstraint("local_port", name="uq_tunnel_port"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("connection_profiles.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    local_port: Mapped[int] = mapped_column(Integer, nullable=False)
    target_host: Mapped[str] = mapped_column(String(255), nullable=False)
    target_port: Mapped[int] = mapped_column(Integer, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


# ---- cameras -----------------------------------------------------------


class Camera(Base, TimestampMixin):
    __tablename__ = "cameras"
    __table_args__ = (UniqueConstraint("team_id", "name", name="uq_camera_name_per_team"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("connection_profiles.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: The customer's own identifier for this camera -- NVR channel, asset tag,
    #: the number on the housing. Not unique and not ours: two sites can use the
    #: same numbering, and it is a label to search by, not a key.
    ref: Mapped[str] = mapped_column(String(120), default="", index=True)
    location: Mapped[str] = mapped_column(String(255), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    profile: Mapped[ConnectionProfile] = relationship(back_populates="cameras")
    sources: Mapped[list[CameraSource]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )


class CameraSource(Base, TimestampMixin):
    """One camera, up to two sources.

    The RTSP feed usually needs the tunnel; the HLS feed -- the inferred one QA
    compares against -- is usually reachable directly. They are separate rows
    because they have separate reachability, separate credentials and separate
    failure modes.
    """

    __tablename__ = "camera_sources"
    __table_args__ = (UniqueConstraint("camera_id", "kind", name="uq_one_source_per_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    kind: Mapped[SourceKind] = mapped_column(String(8), nullable=False)

    #: Credential-free. The real URL is rebuilt at the subprocess boundary.
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(160), default="")
    password_ref: Mapped[str | None] = mapped_column(Text)
    #: When false this source is reached without the profile's tunnel.
    uses_profile_path: Mapped[bool] = mapped_column(Boolean, default=True)

    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_probe_detail: Mapped[str] = mapped_column(Text, default="")
    codec: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)

    camera: Mapped[Camera] = relationship(back_populates="sources")


# ---- recordings --------------------------------------------------------


class Recording(Base, TimestampMixin):
    __tablename__ = "recordings"
    __table_args__ = (Index("ix_recordings_team_state", "team_id", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    state: Mapped[RecordingState] = mapped_column(String(16), default=RecordingState.QUEUED)
    requested_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Seconds of stream actually captured, outages excluded.
    captured_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    gap_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    failure_reason: Mapped[str] = mapped_column(Text, default="")

    segments: Mapped[list[Segment]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    gaps: Mapped[list[Gap]] = relationship(back_populates="recording", cascade="all, delete-orphan")
    objects: Mapped[list[StorageObject]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )


class Segment(Base):
    """One sealed chunk. Written and flushed before the next one starts, which is
    what makes a mid-recording drop cost seconds instead of everything."""

    __tablename__ = "recording_segments"
    __table_args__ = (Index("ix_segments_recording_seq", "recording_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[SourceKind] = mapped_column(String(8), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    recording: Mapped[Recording] = relationship(back_populates="segments")


class Gap(Base):
    """When the stream was not arriving, and why. This table is the answer to
    'was the camera down, or the tunnel?'."""

    __tablename__ = "gaps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[SourceKind] = mapped_column(String(8), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    #: Which layer went away: vpn, ssh, camera, or unknown.
    cause: Mapped[str] = mapped_column(String(32), default="unknown")
    detail: Mapped[str] = mapped_column(Text, default="")
    redial_attempts: Mapped[int] = mapped_column(Integer, default=0)

    recording: Mapped[Recording] = relationship(back_populates="gaps")


class StorageObject(Base, TimestampMixin):
    __tablename__ = "storage_objects"
    __table_args__ = (Index("ix_storage_team_eligible", "team_id", "eligible_for_deletion_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    #: Null for objects that describe the session rather than one source --
    #: gaps.json. They are rows like any other so that retention sweeps them
    #: too; nothing on the gateway expires anything we forget about.
    source_kind: Mapped[SourceKind | None] = mapped_column(String(8))
    s3_key: Mapped[str] = mapped_column(String(700), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(80), default="video/mp4")
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: Set by the retention job a week ahead, so the UI can warn before deleting.
    eligible_for_deletion_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recording: Mapped[Recording] = relationship(back_populates="objects")


class AuditEvent(Base):
    """Append-only. Never updated, never deleted by the application."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_team_time", "team_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    team_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_email: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(48), default="")
    subject_id: Mapped[str | None] = mapped_column(String(36))
    detail: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    source_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

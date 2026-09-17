"""Request and response shapes.

Response models are the last line of defence on credentials: they list fields
explicitly rather than dumping ORM objects, so a new sealed-ref column added to
a model cannot start appearing in API output by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
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


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth --------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class TeamBrief(Model):
    id: str
    name: str
    slug: str


class Me(Model):
    id: str
    email: str
    display_name: str
    role: Role
    teams: list[TeamBrief] = []
    #: Whether this account may change anything. Sent so the dashboard can drop
    #: the controls rather than render them and have the server say no -- a
    #: button that always fails is a worse answer than no button.
    may_write: bool = True


def check_password(value: str) -> str:
    """The one place the password rule lives.

    A length floor and nothing else. Composition rules ("one digit, one
    symbol") push people towards `Password1!` and are worth less than four more
    characters, so the deployment sets a length in MIN_PASSWORD_LENGTH and that
    is the whole policy.
    """
    minimum = settings().min_password_length
    if len(value) < minimum:
        raise ValueError(f"Use at least {minimum} characters.")
    return value


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str

    _password = field_validator("password")(check_password)


class ChangePasswordRequest(BaseModel):
    """Changing your own password. The current one is required.

    Not because the session is untrusted, but because a session cookie left
    open on a shared machine should not be enough to take the account over: the
    old password is the thing the person walking past does not have.
    """

    current_password: str
    password: str

    _password = field_validator("password")(check_password)


class ResetLink(Model):
    """What an administrator gets when they reset somebody's password.

    Returned rather than emailed because most of these deployments have no
    outbound mail, and "the link is in your inbox" is a dead end on an isolated
    network. When SMTP is configured the message goes out as well and
    ``emailed`` says so.
    """

    url: str
    expires_in: int
    emailed: bool


# ---- teams and users ---------------------------------------------------


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""


class TeamOut(TeamBrief):
    description: str = ""
    member_count: int = 0


class UserCreate(BaseModel):
    email: str
    display_name: str = ""
    password: str = Field(description="At least MIN_PASSWORD_LENGTH characters.")
    #: Defaults to the least: an account created without saying what it is for
    #: should be able to watch, not to administer.
    role: Role = Role.VIEWER
    #: Teams to put the new account in. Required of an admin, who may only
    #: create accounts inside their own teams; optional for a superadmin.
    team_ids: list[str] = []

    _password = field_validator("password")(check_password)


class UserOut(Model):
    id: str
    email: str
    display_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None = None


class UserUpdate(BaseModel):
    """What may be changed about an account after it exists."""

    role: Role | None = None
    is_active: bool | None = None
    display_name: str | None = None


class MembershipRequest(BaseModel):
    user_id: str


# ---- connection profiles -----------------------------------------------


#: Fields that are addresses or account names, never prose. Every one of them
#: gets pasted, and a paste brings its surroundings: a leading space in a VPN
#: username is invisible in the form and comes back from the gateway as "check
#: the username and password", which sends the reader looking at the password.
_PASTED = (
    "vpn_gateway",
    "vpn_username",
    "vpn_realm",
    "jump_host",
    "jump_username",
    "whitelist_url",
)


def strip_pasted(value: str | None) -> str | None:
    """Trim a field whose value came off someone's clipboard."""
    return value.strip() if isinstance(value, str) else value


def split_gateway(value: str) -> tuple[str, int | None]:
    """Separate a pasted ``host:port`` into the two fields we store.

    FortiClient labels the field "Remote Gateway" and displays it as
    ``82.197.58.159:20443``, so that whole string is what people paste. Stored
    verbatim it is not a hostname, and the failure surfaces two layers away as
    "Name or service not known" -- a DNS error for something nobody was trying
    to resolve. Splitting it here is cheaper than explaining that.

    Only a trailing all-digit segment after a single colon is treated as a port,
    so an IPv6 literal is left alone rather than mangled.
    """
    host = value.strip()
    if host.count(":") != 1:
        return host, None
    name, _, port = host.partition(":")
    if not port.isdigit() or not name:
        return host, None
    number = int(port)
    if not 1 <= number <= 65535:
        return host, None
    return name, number


class ProfileCreate(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=120)
    mode: ReachMode

    vpn_kind: VpnKind = VpnKind.NONE
    vpn_gateway: str = ""
    vpn_port: int = 443
    vpn_username: str = ""
    vpn_password: str = ""
    vpn_realm: str = ""
    wg_config: str = ""

    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_auth: SshAuth = SshAuth.PASSWORD
    jump_password: str = ""
    jump_private_key: str = ""

    whitelist_url: str | None = None

    @field_validator("vpn_kind")
    @classmethod
    def _vpn_matches_mode(cls, value: VpnKind, info) -> VpnKind:
        mode = info.data.get("mode")
        if mode and ReachMode(mode).has_vpn and value is VpnKind.NONE:
            raise ValueError("this mode needs a VPN type - pick one, or use a mode without a VPN")
        return value

    @field_validator("jump_host")
    @classmethod
    def _jump_present_when_needed(cls, value: str, info) -> str:
        mode = info.data.get("mode")
        if mode and ReachMode(mode).has_jump and not value.strip():
            raise ValueError("this mode needs a jump host address")
        return value

    @field_validator(*_PASTED, mode="before")
    @classmethod
    def _trim(cls, value):
        return strip_pasted(value)

    @model_validator(mode="after")
    def _gateway_port_may_arrive_attached(self) -> ProfileCreate:
        host, port = split_gateway(self.vpn_gateway)
        self.vpn_gateway = host
        # A port in the pasted string is the one the user meant; the field's
        # own value is still the untouched 443 default they never looked at.
        if port is not None:
            self.vpn_port = port
        return self


class ProfileUpdate(BaseModel):
    name: str | None = None
    vpn_gateway: str | None = None
    vpn_port: int | None = None
    vpn_username: str | None = None
    vpn_password: str | None = None
    vpn_realm: str | None = None
    jump_host: str | None = None
    jump_port: int | None = None
    jump_username: str | None = None
    jump_auth: SshAuth | None = None
    jump_password: str | None = None
    jump_private_key: str | None = None
    whitelist_url: str | None = None

    @field_validator(*_PASTED, mode="before")
    @classmethod
    def _trim(cls, value):
        return strip_pasted(value)

    @model_validator(mode="after")
    def _gateway_port_may_arrive_attached(self) -> ProfileUpdate:
        if self.vpn_gateway is None:
            return self
        host, port = split_gateway(self.vpn_gateway)
        self.vpn_gateway = host
        # Only when the paste carried one: an explicit vpn_port in the same
        # request is a deliberate value and must win over an absent one.
        if port is not None and self.vpn_port is None:
            self.vpn_port = port
        return self


class ProfileOut(Model):
    """No credential fields, by construction. Only whether one is set."""

    id: str
    team_id: str
    name: str
    mode: ReachMode
    state: ProfileState
    state_detail: str = ""

    vpn_kind: VpnKind
    vpn_gateway: str = ""
    vpn_port: int = 443
    vpn_username: str = ""
    has_vpn_password: bool = False

    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_auth: SshAuth = SshAuth.PASSWORD
    has_jump_credentials: bool = False

    whitelist_url: str | None = None
    trusted_cert: str | None = None
    trusted_cert_algorithm: str = "sha256"
    trusted_cert_accepted_at: datetime | None = None

    tunnel_ip: str | None = None
    last_connected_at: datetime | None = None


class GateResultOut(BaseModel):
    key: str
    index: int
    title: str
    status: GateStatus
    message: str = ""
    detail: dict[str, Any] = {}
    duration_ms: int = 0


class ConnectResponse(BaseModel):
    attempt_id: str
    state: ProfileState
    gates: list[GateResultOut]
    #: Set when a gate is waiting on the user -- today, a certificate to trust.
    action_required: dict[str, Any] | None = None


class TrustRequest(BaseModel):
    fingerprint: str = Field(min_length=32, max_length=160)

    @field_validator("fingerprint")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.replace(":", "").replace(" ", "").lower()


# ---- cameras -----------------------------------------------------------


class SourceIn(BaseModel):
    kind: SourceKind
    url: str
    username: str = ""
    password: str = ""
    #: False for an HLS feed reachable directly while the RTSP needs the tunnel.
    uses_profile_path: bool = True


class CameraCreate(BaseModel):
    team_id: str
    profile_id: str
    name: str = Field(min_length=1, max_length=160)
    #: The customer's own identifier, kept apart from the name.
    ref: str = Field(default="", max_length=120)
    location: str = ""
    sources: list[SourceIn] = Field(min_length=1)


class SourceOut(Model):
    id: str
    kind: SourceKind
    url: str
    host: str = ""
    port: int = 0
    username: str = ""
    uses_profile_path: bool = True
    last_probe_at: datetime | None = None
    last_probe_ok: bool | None = None
    last_probe_detail: str = ""
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class CameraOut(Model):
    id: str
    team_id: str
    profile_id: str
    #: How this camera is reached, said in one phrase. Carried here so the
    #: cameras page never has to read the profiles API to label a row --
    #: viewers are not allowed there and do not need to be.
    profile_name: str = ""
    profile_mode: ReachMode | None = None
    name: str
    ref: str = ""
    location: str = ""
    is_enabled: bool = True
    sources: list[SourceOut] = []


class CameraPageOut(BaseModel):
    items: list[CameraOut]
    total: int
    page: int
    page_size: int
    pages: int


class CameraStatsOut(BaseModel):
    """The overview's figures, counted in the database rather than the browser."""

    cameras: int
    sources: int
    #: Sources whose last probe succeeded. A source that has never been probed
    #: is not reachable -- ``last_probe_ok`` is null until a gate has run.
    sources_reachable: int


class ImportRequest(BaseModel):
    team_id: str
    profile_id: str
    text: str = Field(min_length=1)
    #: Preview without writing anything.
    dry_run: bool = False


class ImportIssueOut(BaseModel):
    line: int
    value: str
    reason: str


class ImportPreview(BaseModel):
    name: str
    ref: str = ""
    location: str = ""
    sources: list[dict[str, Any]] = []


class ImportResponse(BaseModel):
    summary: str
    created: int = 0
    cameras: list[ImportPreview] = []
    duplicates: list[ImportIssueOut] = []
    rejected: list[ImportIssueOut] = []
    dry_run: bool = False


# ---- recordings --------------------------------------------------------


class RecordingCreate(BaseModel):
    camera_ids: list[str] = Field(min_length=1)
    seconds: int = Field(ge=30, le=900)
    sources: list[SourceKind] | None = Field(
        default=None, description="Defaults to every source the camera has."
    )


class RecordingOut(Model):
    id: str
    team_id: str
    camera_id: str
    #: Resolved alongside the recording so that listing recordings does not mean
    #: fetching the whole camera estate to label the rows. Empty only on a live
    #: event, where the agent publishes what it has in hand; the dashboard keeps
    #: the name it already had rather than blanking the row.
    camera_name: str = ""
    state: RecordingState
    requested_seconds: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    captured_seconds: float = 0.0
    gap_seconds: float = 0.0
    total_bytes: int = 0
    failure_reason: str = ""


class RecordingPageOut(BaseModel):
    items: list[RecordingOut]
    total: int
    page: int
    page_size: int
    pages: int


class DownloadLink(BaseModel):
    #: Null for the session's own sidecar, which belongs to no single source.
    source_kind: SourceKind | None
    filename: str
    bytes: int
    url: str
    expires_in: int


# ---- live preview ------------------------------------------------------


class PreviewStart(BaseModel):
    source_kind: SourceKind | None = Field(
        default=None, description="Defaults to the raw camera feed."
    )


class PreviewOut(BaseModel):
    id: str
    camera_id: str
    source_kind: SourceKind
    #: The MediaMTX path. Random, so knowing a camera id is not enough to watch it.
    path: str
    #: Where the browser negotiates WebRTC. Everything else about the stream is
    #: between the browser and MediaMTX.
    whep_url: str
    started_at: datetime
    expires_at: datetime
    viewers: int = 0
    #: The codec the camera is sending. The browser cannot find this out until
    #: it has negotiated, and a codec it cannot decode fails there as a black
    #: frame -- so it is told in advance and can say so instead.
    codec: str = ""
    #: This view's claim on the stream. Hand it back when closing: the stream is
    #: shared, and one tab closing must not end another's.
    viewer: str = ""


# ---- comparison --------------------------------------------------------


class SpanOut(BaseModel):
    """A stretch of video with no discontinuity, placed on both clocks."""

    media_start: float
    wall_start: float
    seconds: float


class GapMarkOut(BaseModel):
    wall_start: float
    seconds: float
    cause: str
    detail: str = ""


class TrackOut(BaseModel):
    source_kind: SourceKind
    url: str
    expires_in: int
    bytes: int
    captured_seconds: float
    gap_seconds: float
    #: Where this feed starts on the shared clock. Rarely zero for both: the two
    #: ffmpegs are started together but do not connect together.
    starts_at: float
    spans: list[SpanOut]
    gaps: list[GapMarkOut]


class AlignmentOut(BaseModel):
    """What the shared transport can and cannot promise.

    Stated rather than implied, because the whole point of the view is judging
    one feed against the other and a silent alignment error looks exactly like
    the inference being wrong.
    """

    method: Literal["wall_clock"] = "wall_clock"
    accuracy_seconds: float
    note: str


class ComparisonOut(BaseModel):
    recording_id: str
    camera_id: str
    camera_name: str
    state: RecordingState
    requested_seconds: int
    #: The moment the earlier of the two feeds started. Everything else in this
    #: response is seconds from here.
    origin: datetime
    window_seconds: float
    tracks: list[TrackOut]
    alignment: AlignmentOut


# ---- storage -----------------------------------------------------------


class StorageUsage(BaseModel):
    used_bytes: int
    warn_bytes: int
    gc_bytes: int
    hard_bytes: int
    state: Literal["ok", "warning", "collecting", "full"]
    message: str = ""
    by_team: dict[str, int] = {}


class AdmissionOut(BaseModel):
    allowed: bool
    reason: str = ""
    estimated_bytes: int = 0
    headroom_bytes: int = 0

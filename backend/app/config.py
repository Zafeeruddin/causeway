"""Runtime configuration. Everything is env-driven; nothing has a secret default."""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    app_secret_key: str = "dev-only-not-for-production"
    log_level: str = "INFO"

    # Composed from the parts below unless set explicitly. Two copies of one
    # password is a trap: change POSTGRES_PASSWORD alone and the server is
    # rebuilt with the new one while every client still dials with the old,
    # which surfaces as "password authentication failed" for a password nobody
    # is using any more. There is one place to change it.
    database_url: str = ""
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "cam"
    postgres_password: str = "cam"
    postgres_db: str = "cam"
    redis_url: str = "redis://localhost:6379/0"

    #: 32 raw bytes, base64-encoded. Empty is tolerated in dev and refused elsewhere.
    secrets_key: str = ""

    # Hosted Versity Gateway. The AWS_* aliases are accepted so one set of
    # credentials works for the aws CLI and for this app without duplication.
    s3_endpoint_url: str = "https://s3.example.com"
    s3_bucket: str = "cam-recordings"
    s3_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("S3_REGION", "AWS_DEFAULT_REGION", "AWS_REGION"),
    )
    s3_access_key: str = Field(
        default="", validation_alias=AliasChoices("S3_ACCESS_KEY", "AWS_ACCESS_KEY_ID")
    )
    s3_secret_key: str = Field(
        default="", validation_alias=AliasChoices("S3_SECRET_KEY", "AWS_SECRET_ACCESS_KEY")
    )
    #: Versity serves path-style URLs. Virtual-host style resolves
    #: <bucket>.s3.example.com, which does not exist.
    s3_addressing_style: str = "path"
    #: Ask the object store whether it is usable when the dashboard loads, and
    #: put a "service unavailable" screen in front of it when not. Off for a
    #: deployment with no store, or one whose product does not answer
    #: HeadBucket. See app/storage/health.py.
    storage_health_check: bool = True
    #: One answer serves every caller for this long.
    storage_health_ttl_seconds: float = 15.0
    #: No answer within this and the store is reported unavailable.
    storage_health_timeout_seconds: float = 5.0

    storage_warn_bytes: int = 60 * 1024**3
    storage_gc_bytes: int = 90 * 1024**3
    storage_hard_bytes: int = 98 * 1024**3

    record_segment_seconds: int = 10
    record_max_seconds: int = 900
    record_default_seconds: int = 300
    max_streams_per_user: int = 5
    max_concurrent_users: int = 3

    tunnel_port_min: int = 20000
    tunnel_port_max: int = 20099
    ssh_control_dir: str = "/run/cam/ctl"
    netns_prefix: str = "cam"
    #: Networks reachable from inside a VPN namespace over the veth pair rather
    #: than the tunnel. The Docker bridge by default; add the storage subnet here
    #: if something in a namespace ever needs it. Never a default route.
    control_cidrs: str = "172.16.0.0/12"

    #: Where recording segments land before they are shipped to S3.
    work_dir: str = "/var/lib/cam/work"

    # ---- live preview ----
    #: MediaMTX republishes camera streams for the browser. The agent pushes to
    #: the RTSP address from inside a namespace and manages paths over the API.
    mediamtx_api_url: str = "http://mediamtx:9997"
    mediamtx_publish_url: str = "rtsp://mediamtx:8554"
    #: What the browser dials for WebRTC. Empty means "derive it from the address
    #: this request arrived on", which is right for a LAN deployment and wrong
    #: the moment there is a proxy in front -- set it explicitly then.
    preview_public_base: str = ""
    #: No viewers for this long and the stream is dropped. Cameras cap concurrent
    #: sessions hard, so a forgotten tab is a session a recording cannot have.
    preview_idle_seconds: int = 45
    preview_max_seconds: int = 3600
    #: How long to wait for frames to actually reach MediaMTX before giving up.
    preview_ready_seconds: float = 15.0
    preview_reap_seconds: int = 10

    #: Where connecting actually happens. ``agent`` sends it over the command
    #: bus, which is the only arrangement that works once the API and the agent
    #: are separate containers: network namespaces belong to one of them.
    #: ``inproc`` runs it here, for a single-process dev run and the tests.
    connect_mode: Literal["inproc", "agent"] = "inproc"

    #: How often the agent looks for queued recordings. Short because it is the
    #: latency between pressing record and the camera being dialled.
    agent_poll_seconds: float = 2.0
    #: How often retention runs. Versity expires nothing on its own.
    retention_sweep_seconds: int = 300

    # ---- transcoding ----
    # Only H.265-and-similar previews cost anything here: an H.264 preview and
    # every recording are stream copies. See app/recorder/accel.py.
    #: auto | nvidia | cpu. "auto" uses the card if one is visible in here.
    transcode_accel: str = "auto"
    #: Share of the machine this is allowed to spend, per accelerator.
    gpu_budget_percent: float = 80.0
    cpu_budget_percent: float = 80.0
    #: Concurrent NVENC sessions the card will take. Modern drivers lift the old
    #: 3-session consumer cap; lower this if yours has not.
    gpu_sessions: int = 16
    #: Cores one 1080p transcode costs. Measured at 0.55 on ultrafast; the
    #: default is rounded up so a busy machine is refused before it judders.
    cpu_cost_per_stream: float = 0.6

    # ---- accounts and access ----
    #: Failed sign-ins allowed from one address before it is made to wait, and
    #: the window they are counted over. Generous, because the point is to make
    #: a password list take geological time, not to inconvenience someone whose
    #: caps lock is on.
    login_attempts_per_address: int = 20
    login_attempts_per_account: int = 6
    login_attempt_window_seconds: int = 300
    #: Password resets requested from one address per hour.
    reset_requests_per_address: int = 5
    reset_request_window_seconds: int = 3600
    #: How many proxies sit in front of this API. Zero means read the socket
    #: address and ignore X-Forwarded-For -- which is correct and safe when
    #: nothing is proxying, and wrong the moment nginx is, because then every
    #: caller looks like 127.0.0.1 and shares one rate limit. Set it to 1 for a
    #: single nginx. See app/security/throttle.py for why it is not a boolean.
    trusted_proxy_hops: int = 0
    #: Shortest password this deployment accepts. Applies to reset and to the
    #: accounts an administrator creates.
    min_password_length: int = 12
    #: Where this deployment is reached from, used to build reset links.
    #: Empty means links are built from the request, which is right for a LAN
    #: and wrong behind a proxy that rewrites the Host header.
    public_base_url: str = ""

    # ---- outbound mail (optional) ----
    # Set SMTP_HOST and SMTP_FROM and self-service password reset turns on.
    # Leave them and administrators issue reset links by hand instead.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    gate_timeout_seconds: float = 20.0
    vpn_dial_timeout_seconds: float = 45.0
    heartbeat_seconds: int = 30

    @field_validator("secrets_key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        if not v:
            return v
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("SECRETS_KEY must be base64") from exc
        if len(raw) != 32:
            raise ValueError(f"SECRETS_KEY must decode to 32 bytes, got {len(raw)}")
        return v

    @model_validator(mode="after")
    def _compose_database_url(self) -> Settings:
        """Build the DSN from the parts unless one was given outright.

        The password is percent-encoded on the way in: a DSN is a URL, and a
        password containing ``@`` or ``/`` splits it in the wrong place and
        produces a connection error that says nothing about quoting.
        """
        if not self.database_url:
            user = quote(self.postgres_user, safe="")
            password = quote(self.postgres_password, safe="")
            self.database_url = (
                f"postgresql+asyncpg://{user}:{password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return self

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    port_pool: tuple[int, int] = Field(default=(20000, 20099), exclude=True)


@lru_cache
def settings() -> Settings:
    return Settings()

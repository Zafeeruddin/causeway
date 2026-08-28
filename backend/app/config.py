"""Runtime configuration. Everything is env-driven; nothing has a secret default."""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    app_secret_key: str = "dev-only-not-for-production"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://cam:cam@localhost:5432/cam"
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

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    port_pool: tuple[int, int] = Field(default=(20000, 20099), exclude=True)


@lru_cache
def settings() -> Settings:
    return Settings()

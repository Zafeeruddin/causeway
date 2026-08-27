"""Runtime configuration. Everything is env-driven; nothing has a secret default."""

from __future__ import annotations

import base64
from functools import lru_cache

from pydantic import Field, field_validator
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

    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "cam-recordings"
    s3_access_key: str = ""
    s3_secret_key: str = ""

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

    #: Where recording segments land before they are shipped to S3.
    work_dir: str = "/var/lib/cam/work"

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

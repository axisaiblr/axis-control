from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlSettings(BaseSettings):
    """Settings for the axis-control process.

    Read from environment variables prefixed with `AXIS_CONTROL_`, e.g.
    `AXIS_CONTROL_DATABASE_URL`. A `.env` file in the working directory is
    also picked up.
    """

    model_config = SettingsConfigDict(
        env_prefix="AXIS_CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="asyncpg-compatible DSN, e.g. "
        "postgresql://user:pass@host:5432/db",
    )
    nats_url: str = Field(
        default="nats://127.0.0.1:4222",
        description="NATS broker URL.",
    )
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")
    command_timeout_seconds: float = Field(
        default=60.0,
        description="Wait this long for an agent acknowledgement before "
        "marking a pending command as failed.",
    )
    command_sweep_interval_seconds: float = Field(
        default=5.0,
        description="How often the timeout sweeper scans for stuck "
        "pending commands.",
    )
    nats_publish_probe_timeout: float = Field(
        default=0.1,
        description="Per-publish timeout used to detect 'no listeners' "
        "via NATS request/no-responders. Sets a lower bound on the "
        "happy-path publish latency.",
    )
    heartbeat_stale_seconds: float = Field(
        default=30.0,
        description="An instance whose last_heartbeat_at is older than "
        "this many seconds is reported as `reachability: offline`. "
        "Should be a multiple of the agent's heartbeat interval so a "
        "single missed publish does not flip an instance offline.",
    )
    registration_token: str | None = Field(
        default=None,
        description="Shared bootstrap secret an agent must present (as "
        "`Authorization: Bearer <token>`) to call POST /api/instances. "
        "If unset, the registration endpoint refuses every request — "
        "production MUST set this. Generate with e.g. "
        "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`.",
    )

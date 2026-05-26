from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_state_dir() -> Path:
    """Platform-appropriate per-user state directory for axis-agent.

    Linux/Mac: $XDG_STATE_HOME (or ~/.local/state). Windows: %LOCALAPPDATA%
    (or ~/AppData/Local). Falls back to ~/.axis-agent if nothing is set.
    """

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            "~/AppData/Local"
        )
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
            "~/.local/state"
        )
    if not base:
        return Path.home() / ".axis-agent"
    return Path(base) / "axis-agent"


def _default_hostname() -> str:
    return socket.gethostname()


class AgentSettings(BaseSettings):
    """Settings for the axis-agent process.

    Environment variables are prefixed with `AXIS_AGENT_`, e.g.
    `AXIS_AGENT_PROJECT_NAME`. A `.env` file is also picked up.
    """

    model_config = SettingsConfigDict(
        env_prefix="AXIS_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_name: str = Field(
        ...,
        description="Logical project this agent runs on the worker "
        "(e.g. text-assistant). Used at first-time registration.",
    )
    hostname: str = Field(
        default_factory=_default_hostname,
        description="Worker hostname, defaults to OS hostname.",
    )
    control_plane_url: str = Field(
        ...,
        description="Base URL of the axis-control HTTP API, e.g. "
        "http://control.example:8000",
    )
    state_dir: Path = Field(
        default_factory=_default_state_dir,
        description="Directory where the assigned instance_id is "
        "persisted across restarts.",
    )
    instance_id: UUID | None = Field(
        default=None,
        description="Optional UUID override. When set, bypasses both "
        "the persisted state and the self-registration step.",
    )
    register_max_attempts: int = Field(default=5, ge=1)
    register_initial_backoff: float = Field(default=1.0, ge=0.0)
    register_max_backoff: float = Field(default=8.0, ge=0.0)

    nats_url: str = Field(default="nats://127.0.0.1:4222")
    registration_token: str | None = Field(
        default=None,
        description="Shared bootstrap secret presented when self-"
        "registering with the control plane. Must match the value "
        "configured on the control plane as "
        "`AXIS_CONTROL_REGISTRATION_TOKEN`. Required in production; "
        "leave unset only when an `instance_id` override is used.",
    )
    heartbeat_interval_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Interval between heartbeat publishes on "
        "heartbeat.<instance_id>. Should be a small fraction of the "
        "control plane's stale window so a single dropped publish does "
        "not flip the instance offline.",
    )
    compose_mode: Literal["logging", "docker"] = Field(
        default="logging",
        description="`logging` = dry-run (safe default). "
        "`docker` = actually run `docker compose stop/start`.",
    )
    compose_file: Path | None = Field(
        default=None,
        description="Required when compose_mode=docker. Absolute path to the "
        "worker's docker-compose.yml.",
    )
    compose_project: str | None = Field(default=None)
    log_level: str = Field(default="INFO")

    @model_validator(mode="after")
    def _validate_compose(self) -> AgentSettings:
        if self.compose_mode == "docker" and self.compose_file is None:
            raise ValueError(
                "AXIS_AGENT_COMPOSE_FILE must be set when "
                "AXIS_AGENT_COMPOSE_MODE=docker"
            )
        return self

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Settings for the axis-agent process.

    Environment variables are prefixed with `AXIS_AGENT_`, e.g.
    `AXIS_AGENT_INSTANCE_ID`. A `.env` file is also picked up.
    """

    model_config = SettingsConfigDict(
        env_prefix="AXIS_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    instance_id: UUID = Field(
        ...,
        description="UUID assigned by the control plane at registration.",
    )
    nats_url: str = Field(default="nats://127.0.0.1:4222")
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

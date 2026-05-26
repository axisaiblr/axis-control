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

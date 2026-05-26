from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CommandType(StrEnum):
    DISABLE = "disable"
    ENABLE = "enable"


class CommandStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliveryHint(StrEnum):
    """Best-effort indicator of whether a published command reached anyone.

    Returned to the dispatch caller alongside the persisted command. Not
    stored; only meaningful at publish time.
    """

    DELIVERED_NOW = "delivered_now"
    NO_LISTENERS = "no_listeners"
    UNKNOWN = "unknown"


TIMEOUT_FAILURE_REASON = "no_acknowledgement_within_timeout"
"""Stable, machine-readable reason recorded on commands that the sweeper
times out. UI and operator tooling can match on this exact string."""


class CommandMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    instance_id: UUID
    type: CommandType
    issued_at: datetime

    @staticmethod
    def subject_for(instance_id: UUID) -> str:
        return f"commands.{instance_id}"


class StatusMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    instance_id: UUID
    type: CommandType
    status: CommandStatus
    completed_at: datetime
    detail: str | None = None

    @staticmethod
    def subject_for(instance_id: UUID) -> str:
        return f"status.{instance_id}"

    @staticmethod
    def subject_wildcard() -> str:
        return "status.>"


class HeartbeatMessage(BaseModel):
    """Periodic liveness signal published by an agent on
    `heartbeat.<instance_id>`. The control plane updates the matching
    instance's `last_heartbeat_at` and derives the `reachability`
    indicator from it. The optional `metadata` blob is reserved for
    future health payload (CPU, memory, per-project metrics)."""

    model_config = ConfigDict(frozen=True)

    instance_id: UUID
    agent_version: str
    metadata: dict[str, str] | None = None

    @staticmethod
    def subject_for(instance_id: UUID) -> str:
        return f"heartbeat.{instance_id}"

    @staticmethod
    def subject_wildcard() -> str:
        return "heartbeat.>"

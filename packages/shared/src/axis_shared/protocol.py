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
    """Control-plane → agent command on `commands.<instance_id>`.
    `agent_token` is the per-instance secret stamped by the control
    plane from the instance row; the agent verifies it matches its
    own persisted token and drops mismatching commands (#8) — this is
    what stops a third party that can reach NATS from impersonating
    the control plane on an open broker."""

    model_config = ConfigDict(frozen=True)

    command_id: UUID
    instance_id: UUID
    type: CommandType
    issued_at: datetime
    agent_token: str

    @staticmethod
    def subject_for(instance_id: UUID) -> str:
        return f"commands.{instance_id}"


class StatusMessage(BaseModel):
    """Outcome of a command, published by the agent on
    `status.<instance_id>`. `agent_token` is the per-instance secret
    minted at registration; subscribers verify it against the stored
    hash and drop reports with a missing or mismatching token (#8)."""

    model_config = ConfigDict(frozen=True)

    command_id: UUID
    instance_id: UUID
    type: CommandType
    status: CommandStatus
    completed_at: datetime
    agent_token: str
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
    future health payload (CPU, memory, per-project metrics).

    `agent_token` is the per-instance secret minted at registration;
    the control plane verifies it against the stored hash and drops
    heartbeats with a missing or mismatching token (#8)."""

    model_config = ConfigDict(frozen=True)

    instance_id: UUID
    agent_version: str
    agent_token: str
    metadata: dict[str, str] | None = None

    @staticmethod
    def subject_for(instance_id: UUID) -> str:
        return f"heartbeat.{instance_id}"

    @staticmethod
    def subject_wildcard() -> str:
        return "heartbeat.>"

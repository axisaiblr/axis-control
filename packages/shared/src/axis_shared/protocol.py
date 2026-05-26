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

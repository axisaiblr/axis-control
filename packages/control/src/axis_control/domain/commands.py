from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from axis_shared.protocol import CommandStatus, CommandType

__all__ = ["Command", "CommandStatus", "CommandType", "new_command"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class Command:
    id: UUID
    instance_id: UUID
    type: CommandType
    status: CommandStatus
    issued_at: datetime
    completed_at: datetime | None = None


def new_command(instance_id: UUID, type_: CommandType) -> Command:
    return Command(
        id=uuid4(),
        instance_id=instance_id,
        type=type_,
        status=CommandStatus.PENDING,
        issued_at=_utcnow(),
    )

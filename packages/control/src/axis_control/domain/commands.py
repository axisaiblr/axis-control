from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from axis_shared.protocol import (
    TIMEOUT_FAILURE_REASON,
    CommandStatus,
    CommandType,
    DeliveryHint,
)

__all__ = [
    "Command",
    "CommandStatus",
    "CommandType",
    "DeliveryHint",
    "DispatchResult",
    "TIMEOUT_FAILURE_REASON",
    "new_command",
]


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
    failure_reason: str | None = None


@dataclass(slots=True, frozen=True)
class DispatchResult:
    """Outcome of `CommandDispatcher.dispatch`.

    The command is always persisted; the delivery hint reflects whether
    a subscriber was reachable at publish time. The hint is informational
    and never blocks the dispatch — even with `NO_LISTENERS`, the row
    sits as `pending` until the timeout sweeper finalises it.
    """

    command: Command
    delivery: DeliveryHint


def new_command(instance_id: UUID, type_: CommandType) -> Command:
    return Command(
        id=uuid4(),
        instance_id=instance_id,
        type=type_,
        status=CommandStatus.PENDING,
        issued_at=_utcnow(),
    )

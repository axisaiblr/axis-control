from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from axis_control.domain.commands import CommandStatus, CommandType
from axis_control.domain.models import InstanceStatus
from axis_shared.protocol import StatusMessage


class CommandsRepoPort(Protocol):
    async def complete(
        self,
        command_id: UUID,
        completed_at: datetime,
        result: CommandStatus,
    ) -> None: ...


class InstancesRepoPort(Protocol):
    async def update_status(
        self, instance_id: UUID, status: InstanceStatus
    ) -> None: ...


_TYPE_TO_NEW_STATUS: dict[CommandType, InstanceStatus] = {
    CommandType.DISABLE: InstanceStatus.DISABLED,
    CommandType.ENABLE: InstanceStatus.RUNNING,
}


class StatusHandler:
    """Apply an inbound status report to control-plane state.

    On a completed disable/enable command the corresponding instance status
    flips. On a failed command the instance state is left untouched — the
    operator decides what to do next.
    """

    def __init__(
        self,
        commands_repo: CommandsRepoPort,
        instances_repo: InstancesRepoPort,
    ) -> None:
        self._commands_repo = commands_repo
        self._instances_repo = instances_repo

    async def handle(self, message: StatusMessage) -> None:
        await self._commands_repo.complete(
            command_id=message.command_id,
            completed_at=message.completed_at,
            result=message.status,
        )
        if message.status is CommandStatus.COMPLETED:
            new_status = _TYPE_TO_NEW_STATUS.get(message.type)
            if new_status is not None:
                await self._instances_repo.update_status(
                    message.instance_id, new_status
                )

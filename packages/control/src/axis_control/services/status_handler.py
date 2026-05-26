from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import UUID

from axis_control.domain.commands import CommandStatus, CommandType
from axis_control.domain.models import WorkloadState
from axis_shared.protocol import StatusMessage

log = logging.getLogger(__name__)


class CommandsRepoPort(Protocol):
    async def complete_if_pending(
        self,
        command_id: UUID,
        completed_at: datetime,
        result: CommandStatus,
    ) -> bool: ...


class InstancesRepoPort(Protocol):
    async def update_workload_state(
        self, instance_id: UUID, workload_state: WorkloadState
    ) -> None: ...


_TYPE_TO_WORKLOAD_STATE: dict[CommandType, WorkloadState] = {
    CommandType.DISABLE: WorkloadState.DISABLED,
    CommandType.ENABLE: WorkloadState.ENABLED,
}


class StatusHandler:
    """Apply an inbound status report to control-plane state.

    On a completed disable/enable command the corresponding instance
    workload state flips. On a failed command the workload state is left
    untouched — the operator decides what to do next. Reachability is
    driven separately by heartbeats and is not touched here.

    Late status reports (where the command has already reached a terminal
    state, typically via the timeout sweeper) are logged as anomalies and
    discarded: terminal means terminal.
    """

    def __init__(
        self,
        commands_repo: CommandsRepoPort,
        instances_repo: InstancesRepoPort,
    ) -> None:
        self._commands_repo = commands_repo
        self._instances_repo = instances_repo

    async def handle(self, message: StatusMessage) -> None:
        updated = await self._commands_repo.complete_if_pending(
            command_id=message.command_id,
            completed_at=message.completed_at,
            result=message.status,
        )
        if not updated:
            log.warning(
                "late status report for terminal command %s ignored "
                "(instance=%s, reported_status=%s)",
                message.command_id,
                message.instance_id,
                message.status.value,
            )
            return
        if message.status is CommandStatus.COMPLETED:
            new_state = _TYPE_TO_WORKLOAD_STATE.get(message.type)
            if new_state is not None:
                await self._instances_repo.update_workload_state(
                    message.instance_id, new_state
                )

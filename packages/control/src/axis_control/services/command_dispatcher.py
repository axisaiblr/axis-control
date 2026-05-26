from __future__ import annotations

from typing import Protocol
from uuid import UUID

from axis_control.domain.commands import Command, CommandType, new_command


class CommandsRepoPort(Protocol):
    async def save_pending(self, command: Command) -> None: ...


class CommandPublisherPort(Protocol):
    async def publish(self, command: Command) -> None: ...


class CommandDispatcher:
    """Persist the command first, then publish to NATS.

    Order matters: a persisted but un-published command is recoverable; a
    published but un-persisted command would be invisible to the admin UI.
    """

    def __init__(
        self,
        repo: CommandsRepoPort,
        publisher: CommandPublisherPort,
    ) -> None:
        self._repo = repo
        self._publisher = publisher

    async def dispatch(self, instance_id: UUID, type_: CommandType) -> Command:
        command = new_command(instance_id=instance_id, type_=type_)
        await self._repo.save_pending(command)
        await self._publisher.publish(command)
        return command

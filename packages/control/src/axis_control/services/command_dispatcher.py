from __future__ import annotations

from typing import Protocol
from uuid import UUID

from axis_control.domain.commands import (
    Command,
    CommandType,
    DeliveryHint,
    DispatchResult,
    new_command,
)


class CommandsRepoPort(Protocol):
    async def save_pending(self, command: Command) -> None: ...


class CommandPublisherPort(Protocol):
    async def publish(
        self, command: Command, *, agent_token: str
    ) -> DeliveryHint: ...


class InstanceTokenLookupPort(Protocol):
    async def get_agent_token(self, instance_id: UUID) -> str | None: ...


class InstanceNotRegistered(RuntimeError):
    """Raised when a dispatch targets an instance with no agent token —
    either it was never registered, or it predates the auth feature.
    The API layer maps this to 404."""


class CommandDispatcher:
    """Persist the command first, then publish to NATS.

    Order matters: a persisted but un-published command is recoverable
    (the timeout sweeper will fail it explicitly); a published but
    un-persisted command would be invisible to the admin UI.

    Every published command carries the instance's `agent_token` so the
    agent can verify the command really came from the control plane
    (rather than a third party who happens to reach the broker). The
    token is looked up from the instances repo at dispatch time.
    """

    def __init__(
        self,
        repo: CommandsRepoPort,
        publisher: CommandPublisherPort,
        token_lookup: InstanceTokenLookupPort,
    ) -> None:
        self._repo = repo
        self._publisher = publisher
        self._token_lookup = token_lookup

    async def dispatch(
        self, instance_id: UUID, type_: CommandType
    ) -> DispatchResult:
        token = await self._token_lookup.get_agent_token(instance_id)
        if token is None:
            raise InstanceNotRegistered(
                f"instance {instance_id} has no agent_token; refusing "
                "to dispatch a command that cannot be authenticated"
            )
        command = new_command(instance_id=instance_id, type_=type_)
        await self._repo.save_pending(command)
        delivery = await self._publisher.publish(
            command, agent_token=token
        )
        return DispatchResult(command=command, delivery=delivery)

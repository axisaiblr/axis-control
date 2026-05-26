from __future__ import annotations

from nats.aio.client import Client as NatsClient

from axis_control.domain.commands import Command
from axis_shared.protocol import CommandMessage


class NatsCommandPublisher:
    def __init__(self, client: NatsClient) -> None:
        self._client = client

    async def publish(self, command: Command) -> None:
        message = CommandMessage(
            command_id=command.id,
            instance_id=command.instance_id,
            type=command.type,
            issued_at=command.issued_at,
        )
        subject = CommandMessage.subject_for(command.instance_id)
        await self._client.publish(
            subject, message.model_dump_json().encode("utf-8")
        )
        await self._client.flush()

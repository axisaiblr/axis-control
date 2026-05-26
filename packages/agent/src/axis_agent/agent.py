from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from axis_agent.compose_runner import ComposeRunner
from axis_shared.protocol import (
    CommandMessage,
    CommandStatus,
    CommandType,
    StatusMessage,
)

log = logging.getLogger(__name__)


class Agent:
    """Worker-side sidecar.

    Subscribes to `commands.<instance_id>`, translates the command into a
    docker compose action, then publishes the result on
    `status.<instance_id>`. Errors in compose execution surface as a
    `failed` status — never silently swallowed.
    """

    def __init__(
        self,
        instance_id: UUID,
        nats_client: NatsClient,
        compose_runner: ComposeRunner,
    ) -> None:
        self._instance_id = instance_id
        self._nats = nats_client
        self._compose = compose_runner
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = await self._nats.subscribe(
            CommandMessage.subject_for(self._instance_id),
            cb=self._on_command,
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None

    async def _on_command(self, msg: Msg) -> None:
        try:
            command = CommandMessage.model_validate_json(msg.data)
        except Exception:
            log.exception("invalid command payload on %s", msg.subject)
            return

        result, detail = await self._execute(command.type)

        status = StatusMessage(
            command_id=command.command_id,
            instance_id=command.instance_id,
            type=command.type,
            status=result,
            completed_at=datetime.now(timezone.utc),
            detail=detail,
        )
        await self._nats.publish(
            StatusMessage.subject_for(command.instance_id),
            status.model_dump_json().encode("utf-8"),
        )
        await self._nats.flush()

    async def _execute(
        self, command_type: CommandType
    ) -> tuple[CommandStatus, str | None]:
        try:
            if command_type is CommandType.DISABLE:
                await self._compose.stop()
            elif command_type is CommandType.ENABLE:
                await self._compose.start()
            return CommandStatus.COMPLETED, None
        except Exception as exc:  # noqa: BLE001
            log.exception("compose %s failed", command_type.value)
            return CommandStatus.FAILED, repr(exc)

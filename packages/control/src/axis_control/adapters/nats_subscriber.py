from __future__ import annotations

import logging
from typing import Protocol

from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from axis_shared.protocol import StatusMessage

log = logging.getLogger(__name__)


class StatusHandlerPort(Protocol):
    async def handle(self, message: StatusMessage) -> None: ...


class StatusSubscriber:
    def __init__(
        self, client: NatsClient, handler: StatusHandlerPort
    ) -> None:
        self._client = client
        self._handler = handler
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = await self._client.subscribe(
            StatusMessage.subject_wildcard(),
            cb=self._on_message,
        )
        # Block until the broker has acknowledged the SUB so a status
        # message published the instant start() returns is delivered
        # rather than dropped to no-listeners.
        await self._client.flush()

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None

    async def _on_message(self, msg: Msg) -> None:
        try:
            message = StatusMessage.model_validate_json(msg.data)
        except Exception:
            log.exception("invalid status payload on %s", msg.subject)
            return
        try:
            await self._handler.handle(message)
        except Exception:
            log.exception(
                "handler failed for command %s", message.command_id
            )

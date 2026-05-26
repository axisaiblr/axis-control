from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from axis_control.domain.auth import verify_agent_token
from axis_shared.protocol import StatusMessage

log = logging.getLogger(__name__)


class StatusHandlerPort(Protocol):
    async def handle(self, message: StatusMessage) -> None: ...


class TokenStorePort(Protocol):
    async def get_agent_token(self, instance_id: UUID) -> str | None: ...


class StatusSubscriber:
    """Subscribes to `status.>` and forwards verified messages to the
    status handler. Messages with a missing or mismatching
    `agent_token` are dropped before any state mutation — late /
    spoofed reports must never finalise a pending command (#8).
    """

    def __init__(
        self,
        client: NatsClient,
        handler: StatusHandlerPort,
        token_store: TokenStorePort,
    ) -> None:
        self._client = client
        self._handler = handler
        self._token_store = token_store
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
            expected = await self._token_store.get_agent_token(
                message.instance_id
            )
        except Exception:
            log.exception(
                "token lookup failed for status on command %s",
                message.command_id,
            )
            return
        if not verify_agent_token(
            presented=message.agent_token, expected=expected
        ):
            log.warning(
                "dropping status report with invalid token for "
                "command %s (instance=%s)",
                message.command_id,
                message.instance_id,
            )
            return
        try:
            await self._handler.handle(message)
        except Exception:
            log.exception(
                "handler failed for command %s", message.command_id
            )

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from axis_shared.protocol import HeartbeatMessage

log = logging.getLogger(__name__)


class HeartbeatSinkPort(Protocol):
    async def update_last_heartbeat_at(
        self, instance_id: UUID, heartbeat_at: datetime
    ) -> None: ...


class HeartbeatSubscriber:
    """Subscribes to `heartbeat.>` and bumps `last_heartbeat_at` on the
    matching instance row. Receipt time on the control plane is the
    source of truth — not the agent's clock — so freshness comparisons
    don't drift across hosts with skewed clocks.
    """

    def __init__(
        self, client: NatsClient, sink: HeartbeatSinkPort
    ) -> None:
        self._client = client
        self._sink = sink
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = await self._client.subscribe(
            HeartbeatMessage.subject_wildcard(),
            cb=self._on_message,
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None

    async def _on_message(self, msg: Msg) -> None:
        try:
            message = HeartbeatMessage.model_validate_json(msg.data)
        except Exception:
            log.exception("invalid heartbeat payload on %s", msg.subject)
            return
        try:
            await self._sink.update_last_heartbeat_at(
                instance_id=message.instance_id,
                heartbeat_at=datetime.now(timezone.utc),
            )
        except Exception:
            log.exception(
                "failed to record heartbeat for instance %s",
                message.instance_id,
            )

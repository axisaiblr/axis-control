from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from axis_control.domain.auth import verify_agent_token
from axis_shared.protocol import HeartbeatMessage

log = logging.getLogger(__name__)


class HeartbeatSinkPort(Protocol):
    async def update_last_heartbeat_at(
        self, instance_id: UUID, heartbeat_at: datetime
    ) -> None: ...
    async def get_agent_token(self, instance_id: UUID) -> str | None: ...


class HeartbeatSubscriber:
    """Subscribes to `heartbeat.>` and bumps `last_heartbeat_at` on the
    matching instance row. Receipt time on the control plane is the
    source of truth — not the agent's clock — so freshness comparisons
    don't drift across hosts with skewed clocks.

    Per-message authentication (#8): every heartbeat carries the
    per-instance `agent_token` minted at registration. The subscriber
    drops messages whose token does not hash to the digest stored on
    the matching instance row. Drops are logged but never raise — a
    spoofed publisher should be silently ignored, not crash the
    subscriber loop.
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
        # Block until the broker has acknowledged the SUB so a heartbeat
        # published the instant start() returns is delivered rather than
        # dropped to no-listeners.
        await self._client.flush()

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
            expected = await self._sink.get_agent_token(
                message.instance_id
            )
        except Exception:
            log.exception(
                "token lookup failed for instance %s", message.instance_id
            )
            return
        if not verify_agent_token(
            presented=message.agent_token, expected=expected
        ):
            log.warning(
                "dropping heartbeat with invalid token for instance %s",
                message.instance_id,
            )
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

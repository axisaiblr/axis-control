from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from nats.aio.client import Client as NatsClient

from axis_shared.protocol import HeartbeatMessage

log = logging.getLogger(__name__)


class HeartbeatPublisher:
    """Publishes one heartbeat as soon as `start()` is called, then once
    per `interval_seconds`. Lives for the lifetime of the agent process.

    The interval should be a small fraction of the control plane's stale
    window (e.g. ~10 s heartbeat → ~30 s stale) so a single dropped
    publish does not flip an instance offline. The publisher does not
    care whether anyone is subscribed; missed beats are simply lost,
    which is the right semantic — the control plane treats absence as
    `offline`."""

    def __init__(
        self,
        *,
        instance_id: UUID,
        nats_client: NatsClient,
        interval_seconds: float,
        agent_version: str,
    ) -> None:
        self._instance_id = instance_id
        self._nats = nats_client
        self._interval = interval_seconds
        self._agent_version = agent_version
        self._task: asyncio.Task[None] | None = None
        self._subject = HeartbeatMessage.subject_for(instance_id)
        self._payload = HeartbeatMessage(
            instance_id=instance_id,
            agent_version=agent_version,
        ).model_dump_json().encode("utf-8")

    async def start(self) -> None:
        if self._task is not None:
            return
        # Publish once before scheduling the loop so the control plane
        # observes liveness within milliseconds of the agent coming up.
        await self._publish_one()
        self._task = asyncio.create_task(self._loop(), name="agent-heartbeat")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _publish_one(self) -> None:
        try:
            await self._nats.publish(self._subject, self._payload)
        except Exception:
            log.exception("heartbeat publish failed for %s", self._instance_id)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._publish_one()

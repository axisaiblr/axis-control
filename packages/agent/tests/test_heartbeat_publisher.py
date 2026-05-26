"""Issue #3 — agent publishes a heartbeat on `heartbeat.<instance_id>`
immediately after starting and then on a fixed interval."""

from __future__ import annotations

from uuid import uuid4

import pytest
from nats.aio.client import Client as NatsClient

from axis_agent.heartbeat import HeartbeatPublisher
from axis_shared.protocol import HeartbeatMessage


@pytest.mark.asyncio
async def test_publisher_emits_initial_heartbeat_immediately(
    nats_client: NatsClient,
    agent_nc: NatsClient,
) -> None:
    instance_id = uuid4()
    subscription = await nats_client.subscribe(
        HeartbeatMessage.subject_for(instance_id)
    )
    # Make sure the broker actually registers the subscription before
    # the publisher fires its initial beat; otherwise the first publish
    # races the SUB and is silently dropped.
    await nats_client.flush()

    publisher = HeartbeatPublisher(
        instance_id=instance_id,
        nats_client=agent_nc,
        interval_seconds=60.0,  # long; we only want the immediate one
        agent_version="test-0.0.0",
    )
    await publisher.start()
    try:
        msg = await subscription.next_msg(timeout=1.0)
        body = HeartbeatMessage.model_validate_json(msg.data)
        assert body.instance_id == instance_id
        assert body.agent_version == "test-0.0.0"
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_publisher_emits_repeated_heartbeats_on_interval(
    nats_client: NatsClient,
    agent_nc: NatsClient,
) -> None:
    instance_id = uuid4()
    subscription = await nats_client.subscribe(
        HeartbeatMessage.subject_for(instance_id)
    )
    # Make sure the broker actually registers the subscription before
    # the publisher fires its initial beat; otherwise the first publish
    # races the SUB and is silently dropped.
    await nats_client.flush()

    publisher = HeartbeatPublisher(
        instance_id=instance_id,
        nats_client=agent_nc,
        interval_seconds=0.1,
        agent_version="test-0.0.0",
    )
    await publisher.start()
    try:
        # First (immediate), second, third — proves the loop runs.
        for _ in range(3):
            msg = await subscription.next_msg(timeout=1.0)
            body = HeartbeatMessage.model_validate_json(msg.data)
            assert body.instance_id == instance_id
    finally:
        await publisher.stop()

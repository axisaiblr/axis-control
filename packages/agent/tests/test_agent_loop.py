from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from nats.aio.client import Client as NatsClient

from axis_agent.agent import Agent
from axis_shared.protocol import (
    CommandMessage,
    CommandStatus,
    CommandType,
    StatusMessage,
)

AGENT_TOKEN = "agent-token-aaaaaaaaaaaaaaaaaaaaaa"


@dataclass
class FakeComposeRunner:
    """Test double for ComposeRunner. Records calls; never touches docker."""

    stop_calls: int = 0
    start_calls: int = 0
    last_action: str | None = field(default=None)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.last_action = "stop"

    async def start(self) -> None:
        self.start_calls += 1
        self.last_action = "start"


@pytest.mark.asyncio
async def test_agent_executes_disable_and_reports_completed_status(
    nats_client: NatsClient,
    agent_nc: NatsClient,
) -> None:
    instance_id = uuid4()
    fake_compose = FakeComposeRunner()
    agent = Agent(
        instance_id=instance_id,
        nats_client=agent_nc,
        compose_runner=fake_compose,
        agent_token=AGENT_TOKEN,
    )
    await agent.start()
    try:
        status_subscription = await nats_client.subscribe(
            StatusMessage.subject_for(instance_id)
        )

        command = CommandMessage(
            command_id=uuid4(),
            instance_id=instance_id,
            type=CommandType.DISABLE,
            issued_at=datetime.now(timezone.utc),
            agent_token=AGENT_TOKEN,
        )
        await nats_client.publish(
            CommandMessage.subject_for(instance_id),
            command.model_dump_json().encode("utf-8"),
        )

        msg = await status_subscription.next_msg(timeout=2.0)
        status = StatusMessage.model_validate_json(msg.data)

        assert status.command_id == command.command_id
        assert status.instance_id == instance_id
        assert status.type is CommandType.DISABLE
        assert status.status is CommandStatus.COMPLETED
        # The agent must stamp its own token on outbound status so the
        # control plane can verify it.
        assert status.agent_token == AGENT_TOKEN

        assert fake_compose.stop_calls == 1
        assert fake_compose.start_calls == 0
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_agent_drops_command_with_wrong_token(
    nats_client: NatsClient,
    agent_nc: NatsClient,
) -> None:
    """A CommandMessage whose `agent_token` does not match the agent's
    own must be ignored: no compose action runs and no status report
    is emitted. This is what stops a third party reachable to the
    broker from impersonating the control plane."""
    instance_id = uuid4()
    fake_compose = FakeComposeRunner()
    agent = Agent(
        instance_id=instance_id,
        nats_client=agent_nc,
        compose_runner=fake_compose,
        agent_token=AGENT_TOKEN,
    )
    await agent.start()
    try:
        status_subscription = await nats_client.subscribe(
            StatusMessage.subject_for(instance_id)
        )

        spoofed = CommandMessage(
            command_id=uuid4(),
            instance_id=instance_id,
            type=CommandType.DISABLE,
            issued_at=datetime.now(timezone.utc),
            agent_token="not-the-real-token",
        )
        await nats_client.publish(
            CommandMessage.subject_for(instance_id),
            spoofed.model_dump_json().encode("utf-8"),
        )
        await nats_client.flush()

        with pytest.raises(asyncio.TimeoutError):
            await status_subscription.next_msg(timeout=0.5)

        assert fake_compose.stop_calls == 0
        assert fake_compose.start_calls == 0
    finally:
        await agent.stop()

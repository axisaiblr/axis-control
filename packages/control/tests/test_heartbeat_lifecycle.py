"""Issue #3 acceptance lifecycle — end-to-end through real NATS:

- Live agent → `reachability: online` within one heartbeat cycle.
- Stopping the agent → `reachability: offline` within the stale window.
- Issuing a disable while online → `workload_state: disabled`,
  reachability untouched.
- Killing the agent after a successful disable → workload stays
  `disabled`, reachability flips `offline`. Restart → reachability back
  to `online`, workload unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest
from nats.aio.client import Client as NatsClient

from axis_agent.agent import Agent
from axis_agent.heartbeat import HeartbeatPublisher
from axis_control.domain.models import Instance


@dataclass
class FakeComposeRunner:
    stop_calls: int = 0
    start_calls: int = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    async def start(self) -> None:
        self.start_calls += 1


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    timeout: float,
    interval: float = 0.05,
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        if await predicate():
            return
        if loop.time() >= deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(interval)


async def _fetch(
    api_client: httpx.AsyncClient, instance_id: str
) -> dict[str, object]:
    resp = await api_client.get(f"/api/instances/{instance_id}")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_full_reachability_and_workload_lifecycle(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[
        ..., Awaitable[tuple[Instance, str]]
    ],
) -> None:
    instance, agent_token = await given_registered_instance(
        project_name="text-assistant"
    )

    fake_compose = FakeComposeRunner()
    agent = Agent(
        instance_id=instance.id,
        nats_client=nats_client,
        compose_runner=fake_compose,
        agent_token=agent_token,
    )
    await agent.start()

    # Heartbeat interval << stale window (1.0s in the fixture).
    heartbeat = HeartbeatPublisher(
        instance_id=instance.id,
        nats_client=nats_client,
        interval_seconds=0.2,
        agent_version="lifecycle-test",
        agent_token=agent_token,
    )
    await heartbeat.start()

    try:
        # 1. Live agent → reachability online.
        async def reachability_is(value: str) -> bool:
            body = await _fetch(api_client, str(instance.id))
            return body["reachability"] == value

        await _wait_until(lambda: reachability_is("online"), timeout=2.0)
        body = await _fetch(api_client, str(instance.id))
        assert body["workload_state"] == "unknown"

        # 3. Disable while online → workload_state disabled, reachability
        # untouched.
        resp = await api_client.post(
            f"/api/instances/{instance.id}/commands",
            json={"type": "disable"},
        )
        assert resp.status_code == 202

        async def workload_is(value: str) -> bool:
            body = await _fetch(api_client, str(instance.id))
            return body["workload_state"] == value

        await _wait_until(lambda: workload_is("disabled"), timeout=2.0)
        body = await _fetch(api_client, str(instance.id))
        assert body["reachability"] == "online"
        assert fake_compose.stop_calls == 1

        # 4a. Kill the heartbeat publisher (simulates the agent dying)
        # while leaving the disable in place. Workload stays disabled,
        # reachability flips offline once the stale window elapses.
        await heartbeat.stop()
        await _wait_until(lambda: reachability_is("offline"), timeout=4.0)
        body = await _fetch(api_client, str(instance.id))
        assert body["workload_state"] == "disabled"

        # 4b. Restart the heartbeat — reachability flips back online,
        # workload still disabled.
        heartbeat2 = HeartbeatPublisher(
            instance_id=instance.id,
            nats_client=nats_client,
            interval_seconds=0.2,
            agent_version="lifecycle-test",
            agent_token=agent_token,
        )
        await heartbeat2.start()
        try:
            await _wait_until(
                lambda: reachability_is("online"), timeout=2.0
            )
            body = await _fetch(api_client, str(instance.id))
            assert body["workload_state"] == "disabled"
        finally:
            await heartbeat2.stop()
    finally:
        await agent.stop()

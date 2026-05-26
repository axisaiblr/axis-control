"""Issue #3 — agent reachability is driven by periodic heartbeats on
`heartbeat.<instance_id>`. The control plane updates `last_heartbeat_at`
on the matching instance row, and the API derives a `reachability`
indicator (`unknown` / `online` / `offline`) from it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from nats.aio.client import Client as NatsClient

from axis_control.domain.models import Instance
from axis_shared.protocol import HeartbeatMessage


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


@pytest.mark.asyncio
async def test_reachability_flips_to_offline_after_stale_window(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    """One heartbeat lights the instance up as `online`. Once the stale
    window (configured small in the test fixture) elapses without
    another heartbeat, the API reports `reachability: offline` while
    `last_heartbeat_at` stays at the value the heartbeat established."""
    instance = await given_registered_instance(project_name="text-assistant")

    heartbeat = HeartbeatMessage(
        instance_id=instance.id,
        agent_version="test-0.0.0",
    )
    await nats_client.publish(
        HeartbeatMessage.subject_for(instance.id),
        heartbeat.model_dump_json().encode("utf-8"),
    )
    await nats_client.flush()

    async def reachability_is(value: str) -> bool:
        resp = await api_client.get(f"/api/instances/{instance.id}")
        return (
            resp.status_code == 200
            and resp.json()["reachability"] == value
        )

    await _wait_until(lambda: reachability_is("online"), timeout=2.0)

    # The api_client fixture sets heartbeat_stale_seconds=1.0; wait it out.
    await _wait_until(lambda: reachability_is("offline"), timeout=4.0)

    body = (await api_client.get(f"/api/instances/{instance.id}")).json()
    assert body["reachability"] == "offline"
    assert body["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_heartbeat_flips_reachability_from_unknown_to_online(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    """A freshly registered instance shows `reachability: unknown` and a
    null `last_heartbeat_at`. After the agent publishes one heartbeat on
    `heartbeat.<id>`, the API flips to `online` with a populated
    timestamp."""
    instance = await given_registered_instance(project_name="text-assistant")

    initial = await api_client.get(f"/api/instances/{instance.id}")
    assert initial.status_code == 200
    body = initial.json()
    assert body["reachability"] == "unknown"
    assert body["last_heartbeat_at"] is None

    heartbeat = HeartbeatMessage(
        instance_id=instance.id,
        agent_version="test-0.0.0",
    )
    await nats_client.publish(
        HeartbeatMessage.subject_for(instance.id),
        heartbeat.model_dump_json().encode("utf-8"),
    )
    await nats_client.flush()

    async def reachability_is_online() -> bool:
        resp = await api_client.get(f"/api/instances/{instance.id}")
        if resp.status_code != 200:
            return False
        return resp.json()["reachability"] == "online"

    await _wait_until(reachability_is_online, timeout=2.0)

    final = await api_client.get(f"/api/instances/{instance.id}")
    body = final.json()
    assert body["reachability"] == "online"
    assert body["last_heartbeat_at"] is not None

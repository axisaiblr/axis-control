from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx
import pytest
from nats.aio.client import Client as NatsClient

from axis_control.domain.commands import CommandType
from axis_control.domain.models import Instance


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
async def test_status_report_with_wrong_token_leaves_command_pending(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_pending_disable_command: Callable[
        ..., Awaitable[tuple[Instance, str, str]]
    ],
) -> None:
    """A status report with a token that does not match the instance's
    stored hash must not move the command out of `pending` — the report
    is treated as spoofed and dropped before the handler runs."""
    instance, command_id, _legit_token = await given_pending_disable_command(
        project_name="text-assistant"
    )

    spoofed = {
        "command_id": command_id,
        "instance_id": str(instance.id),
        "type": CommandType.DISABLE.value,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "agent_token": "wrong-token-xxx",
    }
    await nats_client.publish(
        f"status.{instance.id}",
        json.dumps(spoofed).encode("utf-8"),
    )
    await nats_client.flush()

    # Give the subscriber time to receive and (correctly) drop it.
    await asyncio.sleep(0.3)

    resp = await api_client.get(f"/api/commands/{command_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending", (
        "spoofed status must not finalise the command"
    )
    instance_resp = await api_client.get(f"/api/instances/{instance.id}")
    assert instance_resp.json()["workload_state"] == "unknown"


@pytest.mark.asyncio
async def test_agent_status_report_completes_command_and_disables_instance(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_pending_disable_command: Callable[
        ..., Awaitable[tuple[Instance, str, str]]
    ],
) -> None:
    instance, command_id, agent_token = await given_pending_disable_command(
        project_name="text-assistant"
    )

    status_payload = {
        "command_id": command_id,
        "instance_id": str(instance.id),
        "type": CommandType.DISABLE.value,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "agent_token": agent_token,
    }
    await nats_client.publish(
        f"status.{instance.id}",
        json.dumps(status_payload).encode("utf-8"),
    )

    async def command_completed() -> bool:
        resp = await api_client.get(f"/api/commands/{command_id}")
        return resp.status_code == 200 and resp.json()["status"] == "completed"

    await _wait_until(command_completed, timeout=2.0)

    instance_resp = await api_client.get(f"/api/instances/{instance.id}")
    assert instance_resp.status_code == 200
    assert instance_resp.json()["workload_state"] == "disabled"

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
async def test_agent_status_report_completes_command_and_disables_instance(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_pending_disable_command: Callable[
        ..., Awaitable[tuple[Instance, str]]
    ],
) -> None:
    instance, command_id = await given_pending_disable_command(
        project_name="text-assistant"
    )

    status_payload = {
        "command_id": command_id,
        "instance_id": str(instance.id),
        "type": CommandType.DISABLE.value,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
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
    assert instance_resp.json()["status"] == "disabled"

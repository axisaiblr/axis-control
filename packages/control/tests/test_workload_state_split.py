"""Issue #3 — workload state is the operator's last-expressed intent
for an instance, orthogonal to reachability. Values: `unknown` (never
commanded), `enabled` (last successful enable), `disabled` (last
successful disable). The legacy single `status` field is gone."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx
import pytest
from nats.aio.client import Client as NatsClient

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
async def test_fresh_instance_has_unknown_workload_state_and_no_status_field(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/instances",
        json={"project_name": "text-assistant", "hostname": "worker-01"},
    )
    body = response.json()
    assert body["workload_state"] == "unknown"
    # The split is not "add reachability alongside status"; status is
    # gone in favour of two named axes.
    assert "status" not in body


@pytest.mark.asyncio
async def test_successful_enable_command_flips_workload_state_to_enabled(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    instance = await given_registered_instance(project_name="text-assistant")

    response = await api_client.post(
        f"/api/instances/{instance.id}/commands",
        json={"type": "enable"},
    )
    command_id = response.json()["id"]

    status_payload = {
        "command_id": command_id,
        "instance_id": str(instance.id),
        "type": "enable",
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    await nats_client.publish(
        f"status.{instance.id}",
        json.dumps(status_payload).encode("utf-8"),
    )

    async def workload_is_enabled() -> bool:
        resp = await api_client.get(f"/api/instances/{instance.id}")
        return resp.json()["workload_state"] == "enabled"

    await _wait_until(workload_is_enabled, timeout=2.0)

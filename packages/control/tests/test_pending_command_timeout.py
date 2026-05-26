"""Regression tests for issue #2 — commands published with no NATS
subscriber must transition out of `pending` within a bounded time and
the publish response must hint at no-listener delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

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
async def test_command_to_unreachable_instance_returns_no_listeners_hint(
    api_client: httpx.AsyncClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    """When no agent is subscribed on `commands.<id>`, the POST response
    must include a machine-readable hint that no listener was reachable."""
    instance = await given_registered_instance(project_name="text-assistant")

    response = await api_client.post(
        f"/api/instances/{instance.id}/commands",
        json={"type": "disable"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["delivery"] == "no_listeners"


@pytest.mark.asyncio
async def test_pending_command_times_out_to_failed_without_agent(
    api_client: httpx.AsyncClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    """The sweeper must move long-pending commands to a terminal `failed`
    state with a stable reason. Instance status must NOT change — we
    don't know what really happened on the (absent) worker."""
    instance = await given_registered_instance(project_name="text-assistant")

    response = await api_client.post(
        f"/api/instances/{instance.id}/commands",
        json={"type": "disable"},
    )
    assert response.status_code == 202
    command_id = response.json()["id"]

    async def command_failed_with_timeout_reason() -> bool:
        resp = await api_client.get(f"/api/commands/{command_id}")
        if resp.status_code != 200:
            return False
        body = resp.json()
        return (
            body["status"] == "failed"
            and body.get("failure_reason") == "no_acknowledgement_within_timeout"
        )

    # api_client fixture wires the sweeper with a very short timeout so
    # this resolves in a few seconds at most.
    await _wait_until(command_failed_with_timeout_reason, timeout=8.0)

    instance_resp = await api_client.get(f"/api/instances/{instance.id}")
    assert instance_resp.status_code == 200
    # Workload state must remain whatever it was before — the worker is
    # silent, we don't infer its state from a lost ack.
    assert (
        instance_resp.json()["workload_state"]
        == instance.workload_state.value
    )


@pytest.mark.asyncio
async def test_late_status_after_timeout_does_not_resurrect_command(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    """If an agent connects after a timeout fires and publishes a success
    status, the command must remain in its terminal `failed` state and
    the instance status must not change. Terminal means terminal."""
    import json
    from datetime import datetime, timezone

    from axis_control.domain.commands import CommandType

    instance = await given_registered_instance(project_name="text-assistant")

    response = await api_client.post(
        f"/api/instances/{instance.id}/commands",
        json={"type": "disable"},
    )
    command_id = response.json()["id"]

    async def command_failed() -> bool:
        resp = await api_client.get(f"/api/commands/{command_id}")
        return resp.status_code == 200 and resp.json()["status"] == "failed"

    await _wait_until(command_failed, timeout=8.0)

    # Now simulate a late, well-formed success report from an agent that
    # finally came online.
    late_payload = {
        "command_id": command_id,
        "instance_id": str(instance.id),
        "type": CommandType.DISABLE.value,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    await nats_client.publish(
        f"status.{instance.id}",
        json.dumps(late_payload).encode("utf-8"),
    )
    await nats_client.flush()

    # Give the status subscriber time to process (and ignore) the late msg.
    await asyncio.sleep(0.3)

    detail = await api_client.get(f"/api/commands/{command_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "failed"
    assert body.get("failure_reason") == "no_acknowledgement_within_timeout"

    instance_resp = await api_client.get(f"/api/instances/{instance.id}")
    assert (
        instance_resp.json()["workload_state"]
        == instance.workload_state.value
    )

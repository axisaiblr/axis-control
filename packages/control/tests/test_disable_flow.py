from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from nats.aio.client import Client as NatsClient

from axis_control.domain.models import Instance
from axis_shared.protocol import CommandMessage, CommandType


@pytest.mark.asyncio
async def test_disable_publishes_command_to_agent_and_persists_as_pending(
    api_client: httpx.AsyncClient,
    nats_client: NatsClient,
    given_registered_instance: Callable[..., Awaitable[Instance]],
) -> None:
    instance = await given_registered_instance(project_name="text-assistant")

    subscription = await nats_client.subscribe(f"commands.{instance.id}")

    response = await api_client.post(
        f"/api/instances/{instance.id}/commands",
        json={"type": "disable"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    command_id = body["id"]
    assert body["status"] == "pending"

    msg = await subscription.next_msg(timeout=2.0)
    payload = CommandMessage.model_validate_json(msg.data)
    assert payload.type is CommandType.DISABLE
    assert str(payload.instance_id) == str(instance.id)
    assert str(payload.command_id) == command_id

    detail = await api_client.get(f"/api/commands/{command_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"

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
    given_registered_instance: Callable[
        ..., Awaitable[tuple[Instance, str]]
    ],
) -> None:
    instance, agent_token = await given_registered_instance(
        project_name="text-assistant"
    )

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
    # Control plane must stamp the published command with the
    # instance's agent_token so the agent can verify the command really
    # came from the control plane (#8).
    assert payload.agent_token == agent_token

    detail = await api_client.get(f"/api/commands/{command_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_command_to_unregistered_instance_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    """Dispatching to an instance id that has no agent_token must
    fail explicitly. Otherwise the control plane would publish an
    un-stampable command that any agent could ignore — silently broken."""
    from uuid import uuid4

    response = await api_client.post(
        f"/api/instances/{uuid4()}/commands", json={"type": "disable"}
    )
    assert response.status_code == 404, response.text

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest

from axis_agent.control_plane import (
    ControlPlaneClient,
    ControlPlaneError,
    ControlPlaneUnreachable,
)


@pytest.mark.asyncio
async def test_register_posts_payload_and_returns_assigned_uuid_with_token() -> None:
    captured: dict[str, object] = {}
    assigned = uuid4()
    minted_token = "opaque-agent-token-1234567890abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            201,
            json={
                "id": str(assigned),
                "project_id": str(uuid4()),
                "project_name": "text-assistant",
                "hostname": "worker-acme-01",
                "workload_state": "unknown",
                "reachability": "unknown",
                "last_heartbeat_at": None,
                "agent_token": minted_token,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(
            http=http, bootstrap_token="bootstrap-secret"
        )
        outcome = await client.register(
            project_name="text-assistant", hostname="worker-acme-01"
        )

    assert isinstance(outcome.instance_id, UUID)
    assert outcome.instance_id == assigned
    assert outcome.agent_token == minted_token
    assert captured["method"] == "POST"
    assert captured["url"] == "http://control/api/instances"
    assert captured["json"] == {
        "project_name": "text-assistant",
        "hostname": "worker-acme-01",
    }
    assert captured["authorization"] == "Bearer bootstrap-secret"


@pytest.mark.asyncio
async def test_register_without_bootstrap_token_omits_authorization() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            201,
            json={
                "id": str(uuid4()),
                "project_id": str(uuid4()),
                "project_name": "text-assistant",
                "hostname": "w",
                "workload_state": "unknown",
                "reachability": "unknown",
                "last_heartbeat_at": None,
                "agent_token": "t",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http, bootstrap_token=None)
        await client.register(project_name="text-assistant", hostname="w")

    assert captured["authorization"] is None


@pytest.mark.asyncio
async def test_register_raises_unreachable_when_transport_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http, bootstrap_token="t")
        with pytest.raises(ControlPlaneUnreachable):
            await client.register(
                project_name="text-assistant", hostname="worker-acme-01"
            )


@pytest.mark.asyncio
async def test_register_raises_error_on_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http, bootstrap_token="wrong")
        with pytest.raises(ControlPlaneError):
            await client.register(
                project_name="text-assistant", hostname="worker-acme-01"
            )


@pytest.mark.asyncio
async def test_register_raises_error_when_agent_token_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": str(uuid4()),
                "project_id": str(uuid4()),
                "project_name": "text-assistant",
                "hostname": "w",
                "workload_state": "unknown",
                "reachability": "unknown",
                "last_heartbeat_at": None,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http, bootstrap_token="t")
        with pytest.raises(ControlPlaneError):
            await client.register(
                project_name="text-assistant", hostname="w"
            )

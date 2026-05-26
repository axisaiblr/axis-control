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
async def test_register_posts_payload_and_returns_assigned_uuid() -> None:
    captured: dict[str, object] = {}
    assigned = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": str(assigned),
                "project_id": str(uuid4()),
                "project_name": "text-assistant",
                "hostname": "worker-acme-01",
                "status": "unknown",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http)
        got = await client.register(
            project_name="text-assistant", hostname="worker-acme-01"
        )

    assert got == assigned
    assert isinstance(got, UUID)
    assert captured["method"] == "POST"
    assert captured["url"] == "http://control/api/instances"
    assert captured["json"] == {
        "project_name": "text-assistant",
        "hostname": "worker-acme-01",
    }


@pytest.mark.asyncio
async def test_register_raises_unreachable_when_transport_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http)
        with pytest.raises(ControlPlaneUnreachable):
            await client.register(
                project_name="text-assistant", hostname="worker-acme-01"
            )


@pytest.mark.asyncio
async def test_register_raises_error_on_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as http:
        client = ControlPlaneClient(http=http)
        with pytest.raises(ControlPlaneError):
            await client.register(
                project_name="text-assistant", hostname="worker-acme-01"
            )

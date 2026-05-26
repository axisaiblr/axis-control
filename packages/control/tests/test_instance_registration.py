from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_registering_instance_makes_it_retrievable(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/instances",
        json={"project_name": "text-assistant", "hostname": "worker-acme-01"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    instance_id = body["id"]
    assert body["project_name"] == "text-assistant"
    assert body["hostname"] == "worker-acme-01"
    assert body["status"] == "unknown"

    get_resp = await api_client.get(f"/api/instances/{instance_id}")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["id"] == instance_id
    assert got["project_name"] == "text-assistant"
    assert got["hostname"] == "worker-acme-01"
    assert got["status"] == "unknown"

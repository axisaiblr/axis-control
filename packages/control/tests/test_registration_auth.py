"""Authentication tests for the bootstrap registration endpoint (#8).

Two layers of identity:

1. A shared **bootstrap registration token** that gates
   `POST /api/instances`. Without it, anyone reachable to the HTTP port
   can register infinite instances; with it, only operators (and the
   agents they provision) can self-register.
2. A **per-instance agent token** minted at registration time and
   stamped in every NATS message thereafter. That layer is tested at
   the subscriber and publisher level — see `test_heartbeat_flow.py`,
   `test_status_report_flow.py`, and the agent-side `test_agent_loop.py`.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from .conftest import REGISTRATION_TOKEN_FOR_TESTS


def _valid_payload(hostname: str = "worker-auth-01") -> dict[str, str]:
    return {"project_name": "text-assistant", "hostname": hostname}


@pytest.mark.asyncio
async def test_registration_without_bearer_token_returns_401(
    api_client_unauthed: httpx.AsyncClient,
) -> None:
    response = await api_client_unauthed.post(
        "/api/instances", json=_valid_payload()
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_registration_with_wrong_bearer_token_returns_401(
    api_client_unauthed: httpx.AsyncClient,
) -> None:
    response = await api_client_unauthed.post(
        "/api/instances",
        json=_valid_payload(),
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_registration_with_valid_bearer_token_succeeds(
    api_client_unauthed: httpx.AsyncClient,
) -> None:
    response = await api_client_unauthed.post(
        "/api/instances",
        json=_valid_payload(),
        headers={
            "Authorization": f"Bearer {REGISTRATION_TOKEN_FOR_TESTS}"
        },
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_registration_response_includes_agent_token(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/instances", json=_valid_payload("worker-token-01")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    token = body.get("agent_token")
    assert isinstance(token, str) and len(token) >= 32, (
        "expected an opaque agent_token of at least 32 chars; "
        f"got {token!r}"
    )


@pytest.mark.asyncio
async def test_two_registrations_mint_distinct_agent_tokens(
    api_client: httpx.AsyncClient,
) -> None:
    a = await api_client.post(
        "/api/instances", json=_valid_payload("worker-token-a")
    )
    b = await api_client.post(
        "/api/instances", json=_valid_payload("worker-token-b")
    )
    assert a.json()["agent_token"] != b.json()["agent_token"]


@pytest.mark.asyncio
async def test_get_instance_does_not_leak_agent_token(
    api_client: httpx.AsyncClient,
) -> None:
    created = (
        await api_client.post(
            "/api/instances", json=_valid_payload("worker-token-leak")
        )
    ).json()
    got = (
        await api_client.get(f"/api/instances/{created['id']}")
    ).json()
    assert "agent_token" not in got, (
        "plaintext token must only be returned at registration time"
    )


@pytest.mark.asyncio
async def test_registration_persists_agent_token_on_instance_row(
    api_client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    body = (
        await api_client.post(
            "/api/instances", json=_valid_payload("worker-token-persist")
        )
    ).json()

    async with db_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT agent_token FROM instances WHERE id = $1", body["id"]
        )
    assert stored == body["agent_token"], (
        "instance row must persist the plaintext token so the control "
        "plane can both verify inbound messages and stamp outbound "
        "commands."
    )

"""End-to-end integration tests for agent self-registration (issue #1).

These tests live in the `control` package because they cross both
layers — they exercise the agent's `ensure_identity` against the real
control-plane HTTP API (real Postgres) and then drive a real `Agent`
through a real NATS subject.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from nats.aio.client import Client as NatsClient

from axis_agent.agent import Agent
from axis_agent.control_plane import ControlPlaneClient
from axis_agent.identity import AgentIdentityStore
from axis_agent.registration import RegistrationInputs, ensure_identity


@dataclass
class FakeComposeRunner:
    stop_calls: int = 0
    start_calls: int = 0
    last_action: str | None = field(default=None)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.last_action = "stop"

    async def start(self) -> None:
        self.start_calls += 1
        self.last_action = "start"


@pytest_asyncio.fixture
async def agent_http(
    api_client: httpx.AsyncClient,
) -> AsyncIterator[ControlPlaneClient]:
    """A ControlPlaneClient bound to the in-process control-plane app."""

    yield ControlPlaneClient(http=api_client)


def _inputs(tmp_path: Path) -> tuple[RegistrationInputs, AgentIdentityStore]:
    inputs = RegistrationInputs(
        project_name="text-assistant",
        hostname="worker-self-reg-01",
        override_instance_id=None,
        max_attempts=3,
        initial_backoff=0.0,
        max_backoff=0.0,
    )
    store = AgentIdentityStore(state_dir=tmp_path)
    return inputs, store


@pytest.mark.asyncio
async def test_self_registered_agent_receives_and_completes_commands(
    api_client: httpx.AsyncClient,
    agent_http: ControlPlaneClient,
    nats_client: NatsClient,
    db_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    inputs, store = _inputs(tmp_path)

    instance_id = await ensure_identity(
        inputs=inputs, store=store, client=agent_http
    )

    # Control plane has a row for the new instance.
    get_resp = await api_client.get(f"/api/instances/{instance_id}")
    assert get_resp.status_code == 200, get_resp.text
    body = get_resp.json()
    assert body["project_name"] == "text-assistant"
    assert body["hostname"] == "worker-self-reg-01"

    # Identity is on disk so a restart would not re-register.
    persisted = store.load()
    assert persisted is not None
    assert persisted.instance_id == instance_id

    # Start the real agent on the auto-assigned id and drive a command.
    fake_compose = FakeComposeRunner()
    agent = Agent(
        instance_id=instance_id,
        nats_client=nats_client,
        compose_runner=fake_compose,
    )
    await agent.start()
    try:
        post = await api_client.post(
            f"/api/instances/{instance_id}/commands",
            json={"type": "disable"},
        )
        assert post.status_code == 202, post.text
        command_id = post.json()["id"]

        # Wait for the agent to ack via the status subject; the control
        # plane subscriber flips the command to completed.
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            resp = await api_client.get(f"/api/commands/{command_id}")
            if resp.status_code == 200 and resp.json()["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("command did not complete in time")

        assert fake_compose.stop_calls == 1

        # Instance row count: exactly one for this hostname.
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM instances WHERE hostname=$1",
                "worker-self-reg-01",
            )
        assert count == 1
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_restart_with_same_state_dir_reuses_instance_id(
    api_client: httpx.AsyncClient,
    agent_http: ControlPlaneClient,
    db_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    inputs, store = _inputs(tmp_path)

    first = await ensure_identity(
        inputs=inputs, store=store, client=agent_http
    )

    # Simulate a restart: brand-new store object, same directory.
    second_store = AgentIdentityStore(state_dir=tmp_path)
    second = await ensure_identity(
        inputs=inputs, store=second_store, client=agent_http
    )

    assert second == first

    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM instances WHERE hostname=$1",
            "worker-self-reg-01",
        )
    assert count == 1


@pytest.mark.asyncio
async def test_reset_identity_causes_re_registration(
    api_client: httpx.AsyncClient,
    agent_http: ControlPlaneClient,
    db_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    inputs, store = _inputs(tmp_path)

    first = await ensure_identity(
        inputs=inputs, store=store, client=agent_http
    )
    store.clear()  # operator deletes state file or passes --reset-identity
    second = await ensure_identity(
        inputs=inputs, store=store, client=agent_http
    )

    assert second != first
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM instances WHERE hostname=$1",
            "worker-self-reg-01",
        )
    assert count == 2


@pytest.mark.asyncio
async def test_explicit_instance_id_override_bypasses_registration(
    agent_http: ControlPlaneClient,
    db_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    inputs, store = _inputs(tmp_path)
    override = uuid4()
    overridden = RegistrationInputs(
        project_name=inputs.project_name,
        hostname=inputs.hostname,
        override_instance_id=override,
        max_attempts=inputs.max_attempts,
        initial_backoff=inputs.initial_backoff,
        max_backoff=inputs.max_backoff,
    )

    resolved = await ensure_identity(
        inputs=overridden, store=store, client=agent_http
    )

    assert resolved == override
    # Nothing persisted; nothing in the DB.
    assert store.load() is None
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM instances WHERE hostname=$1",
            "worker-self-reg-01",
        )
    assert count == 0

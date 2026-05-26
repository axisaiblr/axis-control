from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from axis_agent.control_plane import ControlPlaneUnreachable
from axis_agent.identity import AgentIdentity, AgentIdentityStore
from axis_agent.registration import (
    RegistrationFailed,
    RegistrationInputs,
    ensure_identity,
)


@dataclass
class FakeControlPlaneClient:
    """Test double for ControlPlaneClient.register."""

    next_id: UUID = field(default_factory=uuid4)
    fail_times: int = 0
    calls: int = 0
    last_payload: tuple[str, str] | None = None

    async def register(
        self, *, project_name: str, hostname: str
    ) -> UUID:
        self.calls += 1
        self.last_payload = (project_name, hostname)
        if self.calls <= self.fail_times:
            raise ControlPlaneUnreachable("connection refused (test)")
        return self.next_id


@pytest.fixture
def store(tmp_path: Path) -> AgentIdentityStore:
    return AgentIdentityStore(state_dir=tmp_path)


@pytest.fixture
def inputs() -> RegistrationInputs:
    return RegistrationInputs(
        project_name="text-assistant",
        hostname="worker-acme-01",
        override_instance_id=None,
        max_attempts=3,
        initial_backoff=0.0,
        max_backoff=0.0,
    )


@pytest.mark.asyncio
async def test_override_instance_id_skips_store_and_client(
    store: AgentIdentityStore, inputs: RegistrationInputs
) -> None:
    override = uuid4()
    inputs = replace(inputs, override_instance_id=override)
    persisted = AgentIdentity(
        instance_id=uuid4(),
        project_name="text-assistant",
        hostname="worker-acme-01",
        registered_at=datetime.now(timezone.utc),
    )
    store.save(persisted)
    client = FakeControlPlaneClient()

    resolved = await ensure_identity(
        inputs=inputs, store=store, client=client
    )

    assert resolved == override
    assert client.calls == 0
    assert store.load() == persisted


@pytest.mark.asyncio
async def test_persisted_identity_is_reused_without_calling_client(
    store: AgentIdentityStore, inputs: RegistrationInputs
) -> None:
    persisted = AgentIdentity(
        instance_id=uuid4(),
        project_name="text-assistant",
        hostname="worker-acme-01",
        registered_at=datetime.now(timezone.utc),
    )
    store.save(persisted)
    client = FakeControlPlaneClient()

    resolved = await ensure_identity(
        inputs=inputs, store=store, client=client
    )

    assert resolved == persisted.instance_id
    assert client.calls == 0


@pytest.mark.asyncio
async def test_no_persisted_state_registers_and_persists(
    store: AgentIdentityStore, inputs: RegistrationInputs
) -> None:
    client = FakeControlPlaneClient()

    resolved = await ensure_identity(
        inputs=inputs, store=store, client=client
    )

    assert resolved == client.next_id
    assert client.calls == 1
    assert client.last_payload == ("text-assistant", "worker-acme-01")
    loaded = store.load()
    assert loaded is not None
    assert loaded.instance_id == client.next_id
    assert loaded.project_name == "text-assistant"
    assert loaded.hostname == "worker-acme-01"


@pytest.mark.asyncio
async def test_register_retries_with_bounded_backoff_then_succeeds(
    store: AgentIdentityStore, inputs: RegistrationInputs
) -> None:
    client = FakeControlPlaneClient(fail_times=2)

    resolved = await ensure_identity(
        inputs=inputs, store=store, client=client
    )

    assert resolved == client.next_id
    assert client.calls == 3
    assert store.load() is not None


@pytest.mark.asyncio
async def test_register_gives_up_after_max_attempts(
    store: AgentIdentityStore, inputs: RegistrationInputs
) -> None:
    client = FakeControlPlaneClient(fail_times=10)

    with pytest.raises(RegistrationFailed):
        await ensure_identity(inputs=inputs, store=store, client=client)

    assert client.calls == inputs.max_attempts
    assert store.load() is None

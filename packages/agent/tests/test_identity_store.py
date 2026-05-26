from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from axis_agent.identity import AgentIdentity, AgentIdentityStore


def test_save_then_load_returns_the_same_identity(tmp_path: Path) -> None:
    store = AgentIdentityStore(state_dir=tmp_path)
    identity = AgentIdentity(
        instance_id=uuid4(),
        project_name="text-assistant",
        hostname="worker-acme-01",
        registered_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    store.save(identity)
    loaded = store.load()

    assert loaded == identity


def test_load_on_empty_state_dir_returns_none(tmp_path: Path) -> None:
    store = AgentIdentityStore(state_dir=tmp_path / "fresh")

    assert store.load() is None


def test_clear_removes_the_persisted_identity(tmp_path: Path) -> None:
    store = AgentIdentityStore(state_dir=tmp_path)
    identity = AgentIdentity(
        instance_id=uuid4(),
        project_name="voice-assistant",
        hostname="worker-acme-02",
        registered_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    store.save(identity)

    store.clear()

    assert store.load() is None


def test_save_overwrites_the_prior_identity(tmp_path: Path) -> None:
    store = AgentIdentityStore(state_dir=tmp_path)
    first = AgentIdentity(
        instance_id=uuid4(),
        project_name="text-assistant",
        hostname="worker-acme-01",
        registered_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    second = AgentIdentity(
        instance_id=uuid4(),
        project_name="text-assistant",
        hostname="worker-acme-01",
        registered_at=datetime(2026, 5, 26, 13, 0, 0, tzinfo=timezone.utc),
    )
    store.save(first)

    store.save(second)

    assert store.load() == second

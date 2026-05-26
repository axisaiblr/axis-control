"""Resolve the agent's instance identity at startup.

Three sources are consulted, in priority order:

1. an explicit override (`AXIS_AGENT_INSTANCE_ID`) — wins unconditionally,
   does not persist anything.
2. the on-disk identity store — wins over self-registration when present,
   making restarts idempotent.
3. a fresh `ControlPlaneClient.register` call — retried with bounded
   exponential backoff. On success the result is persisted to the store.

If all retries against the control plane fail, raises
`RegistrationFailed`; callers should treat that as a non-zero exit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from axis_agent.control_plane import ControlPlaneUnreachable
from axis_agent.identity import AgentIdentity, AgentIdentityStore

log = logging.getLogger(__name__)


class RegistrationFailed(RuntimeError):
    """Could not obtain an instance id from any source."""


@dataclass(slots=True, frozen=True)
class RegistrationInputs:
    project_name: str
    hostname: str
    override_instance_id: UUID | None
    max_attempts: int = 5
    initial_backoff: float = 1.0
    max_backoff: float = 8.0


class _RegistersInstances(Protocol):
    async def register(
        self, *, project_name: str, hostname: str
    ) -> UUID: ...


Sleeper = Callable[[float], Awaitable[None]]


async def ensure_identity(
    *,
    inputs: RegistrationInputs,
    store: AgentIdentityStore,
    client: _RegistersInstances,
    sleep: Sleeper = asyncio.sleep,
) -> UUID:
    if inputs.override_instance_id is not None:
        log.info(
            "using override instance_id=%s (skipping store and registration)",
            inputs.override_instance_id,
        )
        return inputs.override_instance_id

    persisted = store.load()
    if persisted is not None:
        log.info(
            "reusing persisted instance_id=%s from %s",
            persisted.instance_id,
            store.path,
        )
        return persisted.instance_id

    log.info(
        "no persisted identity at %s; registering with control plane",
        store.path,
    )
    instance_id = await _register_with_backoff(
        inputs=inputs, client=client, sleep=sleep
    )
    store.save(
        AgentIdentity(
            instance_id=instance_id,
            project_name=inputs.project_name,
            hostname=inputs.hostname,
            registered_at=datetime.now(timezone.utc),
        )
    )
    log.info("registered instance_id=%s; persisted to %s", instance_id, store.path)
    return instance_id


async def _register_with_backoff(
    *,
    inputs: RegistrationInputs,
    client: _RegistersInstances,
    sleep: Sleeper,
) -> UUID:
    delay = inputs.initial_backoff
    last_error: Exception | None = None
    for attempt in range(1, inputs.max_attempts + 1):
        try:
            return await client.register(
                project_name=inputs.project_name,
                hostname=inputs.hostname,
            )
        except ControlPlaneUnreachable as exc:
            last_error = exc
            if attempt >= inputs.max_attempts:
                break
            log.warning(
                "control plane unreachable on attempt %d/%d; retrying in %.1fs",
                attempt,
                inputs.max_attempts,
                delay,
            )
            await sleep(delay)
            delay = min(delay * 2 if delay > 0 else 0.0, inputs.max_backoff)
    raise RegistrationFailed(
        f"control plane unreachable after {inputs.max_attempts} attempts: "
        f"{last_error!r}"
    )

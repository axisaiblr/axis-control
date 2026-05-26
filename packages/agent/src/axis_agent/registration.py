"""Resolve the agent's instance identity at startup.

Three sources are consulted, in priority order:

1. an explicit override (`AXIS_AGENT_INSTANCE_ID`) — wins unconditionally,
   does not persist anything. No agent token is available, so override
   mode is only useful when the operator is willing to forgo the
   message-level auth for this run (e.g. local dev).
2. the on-disk identity store — wins over self-registration when present,
   making restarts idempotent. The persisted `agent_token` is reused.
3. a fresh `ControlPlaneClient.register` call — retried with bounded
   exponential backoff. On success the assigned id and minted token are
   persisted to the store.

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

from axis_agent.control_plane import (
    ControlPlaneUnreachable,
    RegistrationOutcome,
)
from axis_agent.identity import AgentIdentity, AgentIdentityStore

log = logging.getLogger(__name__)

# Sentinel used in the override path where no token is available. The
# agent will still subscribe and run commands, but the control plane
# will drop any heartbeats / status reports it publishes (token
# mismatch). Override mode is for dev only; production must let the
# agent self-register so a real token gets minted.
OVERRIDE_AGENT_TOKEN = ""


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
    ) -> RegistrationOutcome: ...


Sleeper = Callable[[float], Awaitable[None]]


async def ensure_identity(
    *,
    inputs: RegistrationInputs,
    store: AgentIdentityStore,
    client: _RegistersInstances,
    sleep: Sleeper = asyncio.sleep,
) -> AgentIdentity:
    if inputs.override_instance_id is not None:
        log.info(
            "using override instance_id=%s (skipping store and registration)",
            inputs.override_instance_id,
        )
        return AgentIdentity(
            instance_id=inputs.override_instance_id,
            project_name=inputs.project_name,
            hostname=inputs.hostname,
            registered_at=datetime.now(timezone.utc),
            agent_token=OVERRIDE_AGENT_TOKEN,
        )

    persisted = store.load()
    if persisted is not None:
        log.info(
            "reusing persisted instance_id=%s from %s",
            persisted.instance_id,
            store.path,
        )
        return persisted

    log.info(
        "no persisted identity at %s; registering with control plane",
        store.path,
    )
    outcome = await _register_with_backoff(
        inputs=inputs, client=client, sleep=sleep
    )
    identity = AgentIdentity(
        instance_id=outcome.instance_id,
        project_name=inputs.project_name,
        hostname=inputs.hostname,
        registered_at=datetime.now(timezone.utc),
        agent_token=outcome.agent_token,
    )
    store.save(identity)
    log.info(
        "registered instance_id=%s; persisted to %s",
        identity.instance_id,
        store.path,
    )
    return identity


async def _register_with_backoff(
    *,
    inputs: RegistrationInputs,
    client: _RegistersInstances,
    sleep: Sleeper,
) -> RegistrationOutcome:
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

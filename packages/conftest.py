"""Test fixtures shared across all workspace packages.

What lives here: infrastructure that is expensive to spin up (Docker
containers) or that more than one package needs (NATS). Package-specific
fixtures (FastAPI app wiring, DB schema, agent helpers) stay in each
package's own conftest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import nats
import pytest
import pytest_asyncio
from nats.aio.client import Client as NatsClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

# Windows + Docker Desktop: get_container_host_ip() returns "localhost",
# which on Windows resolves IPv6 first and adds a ~5s stall to async
# TCP connects. Force IPv4 loopback so connects are sub-millisecond.
HOST = "127.0.0.1"


@pytest.fixture(scope="session")
def nats_container() -> AsyncIterator[DockerContainer]:
    container = (
        DockerContainer("nats:2.10-alpine")
        .with_exposed_ports(4222)
        .waiting_for(LogMessageWaitStrategy("Server is ready"))
    )
    with container as c:
        yield c


@pytest_asyncio.fixture(scope="session")
async def nats_url(nats_container: DockerContainer) -> str:
    port = nats_container.get_exposed_port(4222)
    url = f"nats://{HOST}:{port}"
    last_err: Exception | None = None
    for _ in range(20):
        try:
            nc = await nats.connect(
                url, connect_timeout=2, max_reconnect_attempts=1
            )
            await nc.close()
            return url
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            await asyncio.sleep(0.25)
    raise RuntimeError(f"NATS at {url} not reachable: {last_err!r}")


@pytest_asyncio.fixture
async def nats_client(nats_url: str) -> AsyncIterator[NatsClient]:
    nc = await nats.connect(
        nats_url, connect_timeout=2, max_reconnect_attempts=1
    )
    try:
        yield nc
    finally:
        await nc.close()

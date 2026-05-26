from __future__ import annotations

from collections.abc import AsyncIterator

import nats
import pytest_asyncio
from nats.aio.client import Client as NatsClient


@pytest_asyncio.fixture
async def agent_nc(nats_url: str) -> AsyncIterator[NatsClient]:
    nc = await nats.connect(
        nats_url, connect_timeout=2, max_reconnect_attempts=1
    )
    try:
        yield nc
    finally:
        # Use close() not drain(). drain() does an UNSUB-then-close that
        # leaves the read loop running while pong futures are cancelled,
        # which races _read_loop's PONG handler and surfaces as
        # `InvalidStateError: invalid state` at teardown.
        await nc.close()

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
        await nc.drain()

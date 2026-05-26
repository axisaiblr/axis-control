from __future__ import annotations

from typing import Protocol


class ComposeRunner(Protocol):
    """Worker-local docker compose interface.

    Implementations call into `docker compose -f <file> stop/start` against
    the worker's compose stack. Tests substitute a fake.
    """

    async def stop(self) -> None: ...
    async def start(self) -> None: ...

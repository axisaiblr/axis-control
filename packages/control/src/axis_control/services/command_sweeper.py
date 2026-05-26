from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from axis_shared.protocol import TIMEOUT_FAILURE_REASON

log = logging.getLogger(__name__)


class CommandsRepoPort(Protocol):
    async def fail_pending_older_than(
        self,
        deadline: datetime,
        completed_at: datetime,
        reason: str,
    ) -> list[UUID]: ...


class CommandTimeoutSweeper:
    """Periodically fail commands stuck in `pending` past the timeout.

    Decouples command terminality from the agent: if no agent ever
    publishes a `StatusMessage` (worker offline, NATS subscription dropped
    after the publish, agent crashed mid-handle), the row is still moved
    to a terminal `failed` state with a stable reason within
    `timeout_seconds + sweep_interval_seconds` of being issued.

    Importantly, instance status is *not* flipped on timeout. We don't
    know what actually happened on the worker, so the safest state is
    "leave the instance status as it was and surface the failure on the
    command".
    """

    def __init__(
        self,
        commands_repo: CommandsRepoPort,
        timeout_seconds: float,
        sweep_interval_seconds: float,
    ) -> None:
        self._repo = commands_repo
        self._timeout = timedelta(seconds=timeout_seconds)
        self._interval = sweep_interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._loop(), name="command-timeout-sweeper"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def sweep_once(self) -> list[UUID]:
        """Run a single sweep pass. Exposed for tests; production calls
        this from `_loop`."""
        now = datetime.now(timezone.utc)
        deadline = now - self._timeout
        failed_ids = await self._repo.fail_pending_older_than(
            deadline=deadline,
            completed_at=now,
            reason=TIMEOUT_FAILURE_REASON,
        )
        if failed_ids:
            log.info(
                "timeout sweeper failed %d pending command(s): %s",
                len(failed_ids),
                ", ".join(str(cid) for cid in failed_ids),
            )
        return failed_ids

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("command sweeper iteration failed")
            await asyncio.sleep(self._interval)

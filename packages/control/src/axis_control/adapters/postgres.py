from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg

from axis_control.domain.commands import Command, CommandStatus, CommandType
from axis_control.domain.models import Instance, InstanceStatus, Project


class CommandsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save_pending(self, command: Command) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO commands "
                "(id, instance_id, type, status, issued_at, completed_at, failure_reason) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                command.id,
                command.instance_id,
                command.type.value,
                command.status.value,
                command.issued_at,
                command.completed_at,
                command.failure_reason,
            )

    async def complete_if_pending(
        self,
        command_id: UUID,
        completed_at: datetime,
        result: CommandStatus,
    ) -> bool:
        """Mark a command terminal only if it is still pending.

        Returns True iff the row was updated. A False return means the
        command was already in a terminal state (failed by the timeout
        sweeper, or completed by an earlier status report) and the caller
        should treat the inbound message as a late / duplicate signal.
        """
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE commands SET status = $1, completed_at = $2 "
                "WHERE id = $3 AND status = $4",
                result.value,
                completed_at,
                command_id,
                CommandStatus.PENDING.value,
            )
        # asyncpg returns the command tag, e.g. "UPDATE 1" / "UPDATE 0".
        _, _, count = status.rpartition(" ")
        return count == "1"

    async def fail_pending_older_than(
        self,
        deadline: datetime,
        completed_at: datetime,
        reason: str,
    ) -> list[UUID]:
        """Atomically flip all pending commands issued before `deadline`
        to `failed` with the given reason. Returns the IDs that flipped."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE commands "
                "SET status = $1, completed_at = $2, failure_reason = $3 "
                "WHERE status = $4 AND issued_at < $5 "
                "RETURNING id",
                CommandStatus.FAILED.value,
                completed_at,
                reason,
                CommandStatus.PENDING.value,
                deadline,
            )
        return [row["id"] for row in rows]

    async def get(self, command_id: UUID) -> Command | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, instance_id, type, status, issued_at, completed_at, "
                "failure_reason "
                "FROM commands WHERE id = $1",
                command_id,
            )
        if row is None:
            return None
        return Command(
            id=row["id"],
            instance_id=row["instance_id"],
            type=CommandType(row["type"]),
            status=CommandStatus(row["status"]),
            issued_at=row["issued_at"],
            completed_at=row["completed_at"],
            failure_reason=row["failure_reason"],
        )


class ProjectsRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def find_or_create_by_name(self, name: str) -> Project:
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO projects (id, name, created_at) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (name) DO NOTHING "
                "RETURNING id, name, created_at",
                uuid4(),
                name,
                now,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id, name, created_at FROM projects "
                    "WHERE name = $1",
                    name,
                )
        assert row is not None
        return Project(
            id=row["id"], name=row["name"], created_at=row["created_at"]
        )


class InstancesRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def save(self, instance: Instance) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO instances "
                "(id, project_id, project_name, hostname, status, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                instance.id,
                instance.project_id,
                instance.project_name,
                instance.hostname,
                instance.status.value,
                instance.created_at,
            )

    async def get(self, instance_id: UUID) -> Instance | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, project_id, project_name, hostname, status, created_at "
                "FROM instances WHERE id = $1",
                instance_id,
            )
        if row is None:
            return None
        return Instance(
            id=row["id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            hostname=row["hostname"],
            status=InstanceStatus(row["status"]),
            created_at=row["created_at"],
        )

    async def update_status(
        self, instance_id: UUID, status: InstanceStatus
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE instances SET status = $1 WHERE id = $2",
                status.value,
                instance_id,
            )

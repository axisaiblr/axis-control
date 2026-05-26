"""DDL for the control plane schema.

This is intentionally not Alembic-managed yet — at the current scale it is
faster to keep a single idempotent DDL block applied at startup. When the
schema starts evolving we add Alembic; until then this file IS the schema.
"""

from __future__ import annotations

import asyncpg

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    project_name TEXT NOT NULL,
    hostname TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id UUID PRIMARY KEY,
    instance_id UUID NOT NULL REFERENCES instances(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
"""


async def apply_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)

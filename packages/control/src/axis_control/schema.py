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
    workload_state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_heartbeat_at TIMESTAMPTZ,
    agent_token TEXT
);

-- Migration from pre-#3 schema: single `status` column carrying
-- {unknown,running,disabled} → split into `workload_state`
-- ({unknown,enabled,disabled}) plus the heartbeat-derived reachability.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'instances' AND column_name = 'status'
    ) THEN
        ALTER TABLE instances
            ADD COLUMN IF NOT EXISTS workload_state TEXT;
        UPDATE instances
        SET workload_state = CASE status
            WHEN 'running' THEN 'enabled'
            ELSE status
        END
        WHERE workload_state IS NULL;
        ALTER TABLE instances ALTER COLUMN workload_state SET NOT NULL;
        ALTER TABLE instances DROP COLUMN status;
    END IF;
END$$;

ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

-- #8: per-instance agent token. Nullable because instances registered
-- before this column existed have no token; they must re-register to
-- acquire one and resume publishing on `status.<id>` / `heartbeat.<id>`.
ALTER TABLE instances ADD COLUMN IF NOT EXISTS agent_token TEXT;

CREATE TABLE IF NOT EXISTS commands (
    id UUID PRIMARY KEY,
    instance_id UUID NOT NULL REFERENCES instances(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    failure_reason TEXT
);

ALTER TABLE commands ADD COLUMN IF NOT EXISTS failure_reason TEXT;
"""


async def apply_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import httpx
import nats
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from axis_control.adapters.nats_heartbeat import HeartbeatSubscriber
from axis_control.adapters.nats_subscriber import StatusSubscriber
from axis_control.api.app import create_app
from axis_control.domain.commands import (
    CommandStatus,
    CommandType,
    new_command,
)
from axis_control.domain.models import Instance, new_instance, new_project
from axis_control.schema import SCHEMA_DDL
from axis_control.services.command_sweeper import CommandTimeoutSweeper
from axis_control.services.status_handler import StatusHandler

# Mirror of packages/conftest.py HOST — pytest conftests aren't importable.
HOST = "127.0.0.1"

# Shared registration bootstrap token used by every authenticated test
# call. Real deployments mint their own; the value here is opaque to
# production code and only meaningful to tests.
REGISTRATION_TOKEN_FOR_TESTS = "test-bootstrap-registration-token"


@pytest.fixture(scope="session")
def postgres_container() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest_asyncio.fixture(scope="session")
async def postgres_dsn(postgres_container: PostgresContainer) -> str:
    port = postgres_container.get_exposed_port(5432)
    user = postgres_container.username
    password = postgres_container.password
    db = postgres_container.dbname
    dsn = f"postgresql://{user}:{password}@{HOST}:{port}/{db}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA_DDL)
    finally:
        await conn.close()
    return dsn


@pytest_asyncio.fixture
async def db_pool(postgres_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=4)
    assert pool is not None
    try:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE commands, instances, projects CASCADE")
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def api_client(
    db_pool: asyncpg.Pool, nats_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    app_nc = await nats.connect(
        nats_url, connect_timeout=2, max_reconnect_attempts=1
    )
    try:
        # Short probe timeout keeps the no-listener happy-path test snappy;
        # a short command timeout + sweep interval keeps the timeout
        # regression tests under a few seconds.
        app = create_app(
            db_pool=db_pool,
            nats_client=app_nc,
            publish_probe_timeout=0.05,
            heartbeat_stale_seconds=1.0,
            registration_token=REGISTRATION_TOKEN_FOR_TESTS,
        )
        handler = StatusHandler(
            commands_repo=app.state.commands_repo,
            instances_repo=app.state.instances_repo,
        )
        subscriber = StatusSubscriber(
            app_nc, handler, token_store=app.state.instances_repo
        )
        await subscriber.start()
        heartbeat_subscriber = HeartbeatSubscriber(
            app_nc, app.state.instances_repo
        )
        await heartbeat_subscriber.start()
        sweeper = CommandTimeoutSweeper(
            commands_repo=app.state.commands_repo,
            timeout_seconds=2.0,
            sweep_interval_seconds=0.1,
        )
        await sweeper.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={
                    "Authorization": f"Bearer {REGISTRATION_TOKEN_FOR_TESTS}"
                },
            ) as client:
                yield client
        finally:
            await sweeper.stop()
            await heartbeat_subscriber.stop()
            await subscriber.stop()
    finally:
        await app_nc.drain()


@pytest_asyncio.fixture
async def api_client_unauthed(
    api_client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    """Same backend as `api_client` but with no default Authorization
    header. Use for 401 / wrong-token assertions."""
    transport = api_client._transport
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def given_registered_instance(
    db_pool: asyncpg.Pool,
) -> Callable[..., Awaitable[tuple[Instance, str]]]:
    """Factory: insert a fully-provisioned instance row directly into
    the DB (skipping the registration HTTP endpoint) and return both
    the domain `Instance` and its plaintext `agent_token`. Tests that
    publish to `status.<id>` / `heartbeat.<id>` need the token to pass
    the control plane's message-level auth (#8)."""

    from axis_control.domain.auth import mint_agent_token

    async def _factory(
        project_name: str, hostname: str = "worker-01"
    ) -> tuple[Instance, str]:
        project = new_project(name=project_name)
        token = mint_agent_token()
        instance = new_instance(
            project, hostname=hostname, agent_token=token
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO projects (id, name, created_at) VALUES ($1, $2, $3)",
                project.id,
                project.name,
                project.created_at,
            )
            await conn.execute(
                "INSERT INTO instances "
                "(id, project_id, project_name, hostname, workload_state, "
                "created_at, agent_token) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                instance.id,
                instance.project_id,
                instance.project_name,
                instance.hostname,
                instance.workload_state.value,
                instance.created_at,
                instance.agent_token,
            )
        return instance, token

    return _factory


@pytest_asyncio.fixture
async def given_pending_disable_command(
    db_pool: asyncpg.Pool,
    given_registered_instance: Callable[..., Awaitable[tuple[Instance, str]]],
) -> Callable[..., Awaitable[tuple[Instance, str, str]]]:
    async def _factory(
        project_name: str, hostname: str = "worker-01"
    ) -> tuple[Instance, str, str]:
        instance, agent_token = await given_registered_instance(
            project_name=project_name, hostname=hostname
        )
        command = new_command(
            instance_id=instance.id, type_=CommandType.DISABLE
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO commands "
                "(id, instance_id, type, status, issued_at, completed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                command.id,
                command.instance_id,
                command.type.value,
                CommandStatus.PENDING.value,
                command.issued_at,
                None,
            )
        return instance, str(command.id), agent_token

    return _factory

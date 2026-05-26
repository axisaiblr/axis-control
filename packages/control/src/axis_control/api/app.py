from __future__ import annotations

import asyncpg
from fastapi import FastAPI
from nats.aio.client import Client as NatsClient

from axis_control.adapters.nats_publisher import NatsCommandPublisher
from axis_control.adapters.postgres import (
    CommandsRepository,
    InstancesRepository,
    ProjectsRepository,
)
from axis_control.api.commands import router as commands_router
from axis_control.api.instances import router as instances_router
from axis_control.services.command_dispatcher import CommandDispatcher
from axis_control.services.registration import RegistrationService


def create_app(
    *,
    db_pool: asyncpg.Pool,
    nats_client: NatsClient,
    publish_probe_timeout: float = 0.1,
) -> FastAPI:
    """Build the FastAPI application.

    Dependencies are passed in (db pool, NATS client) so the caller owns
    their lifecycle. The production entrypoint and tests both open these
    resources before calling `create_app` and tear them down after the
    app is gone. Background subscribers (status reports) and tasks
    (command timeout sweeper) are started separately — see
    `axis_control.adapters.nats_subscriber` and
    `axis_control.services.command_sweeper`.
    """
    app = FastAPI(title="axis-control")

    commands_repo = CommandsRepository(db_pool)
    instances_repo = InstancesRepository(db_pool)
    projects_repo = ProjectsRepository(db_pool)
    publisher = NatsCommandPublisher(
        nats_client, probe_timeout=publish_probe_timeout
    )
    app.state.commands_repo = commands_repo
    app.state.instances_repo = instances_repo
    app.state.projects_repo = projects_repo
    app.state.command_dispatcher = CommandDispatcher(
        repo=commands_repo, publisher=publisher
    )
    app.state.registration_service = RegistrationService(
        projects_repo=projects_repo, instances_repo=instances_repo
    )

    app.include_router(commands_router)
    app.include_router(instances_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app

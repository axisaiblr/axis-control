from __future__ import annotations

import asyncio
import logging
import signal

import asyncpg
import nats
import uvicorn

from axis_control.adapters.nats_heartbeat import HeartbeatSubscriber
from axis_control.adapters.nats_subscriber import StatusSubscriber
from axis_control.api.app import create_app
from axis_control.config import ControlSettings
from axis_control.schema import apply_schema
from axis_control.services.command_sweeper import CommandTimeoutSweeper
from axis_control.services.status_handler import StatusHandler

log = logging.getLogger(__name__)


async def _run(settings: ControlSettings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("axis-control starting on %s:%d", settings.http_host, settings.http_port)

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=8
    )
    assert pool is not None
    await apply_schema(pool)
    log.info("schema applied")

    nc = await nats.connect(
        settings.nats_url, connect_timeout=5, max_reconnect_attempts=-1
    )
    log.info("connected to NATS at %s", settings.nats_url)

    app = create_app(
        db_pool=pool,
        nats_client=nc,
        publish_probe_timeout=settings.nats_publish_probe_timeout,
        heartbeat_stale_seconds=settings.heartbeat_stale_seconds,
    )
    handler = StatusHandler(
        commands_repo=app.state.commands_repo,
        instances_repo=app.state.instances_repo,
    )
    subscriber = StatusSubscriber(nc, handler)
    await subscriber.start()
    log.info("status subscriber attached")

    heartbeat_subscriber = HeartbeatSubscriber(nc, app.state.instances_repo)
    await heartbeat_subscriber.start()
    log.info(
        "heartbeat subscriber attached (stale after %.1fs)",
        settings.heartbeat_stale_seconds,
    )

    sweeper = CommandTimeoutSweeper(
        commands_repo=app.state.commands_repo,
        timeout_seconds=settings.command_timeout_seconds,
        sweep_interval_seconds=settings.command_sweep_interval_seconds,
    )
    await sweeper.start()
    log.info(
        "command timeout sweeper started (timeout=%.1fs interval=%.1fs)",
        settings.command_timeout_seconds,
        settings.command_sweep_interval_seconds,
    )

    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        access_log=True,
    )
    server = uvicorn.Server(config)

    # Wire graceful shutdown on SIGINT / SIGTERM.
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows + ProactorEventLoop: signal handlers are limited.
            pass

    serve_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        server.should_exit = True
        await serve_task

    log.info("shutting down")
    await sweeper.stop()
    await heartbeat_subscriber.stop()
    await subscriber.stop()
    await nc.drain()
    await pool.close()


def cli_main() -> None:
    settings = ControlSettings()
    asyncio.run(_run(settings))


if __name__ == "__main__":
    cli_main()

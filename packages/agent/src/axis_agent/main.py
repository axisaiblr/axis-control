from __future__ import annotations

import asyncio
import logging
import signal

import nats

from axis_agent.agent import Agent
from axis_agent.compose_runner import ComposeRunner
from axis_agent.config import AgentSettings
from axis_agent.runners import DockerComposeRunner, LoggingComposeRunner

log = logging.getLogger(__name__)


def _build_runner(settings: AgentSettings) -> ComposeRunner:
    if settings.compose_mode == "docker":
        assert settings.compose_file is not None  # validated in config
        return DockerComposeRunner(
            compose_file=settings.compose_file,
            project_name=settings.compose_project,
        )
    return LoggingComposeRunner()


async def _run(settings: AgentSettings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info(
        "axis-agent starting instance=%s mode=%s",
        settings.instance_id,
        settings.compose_mode,
    )

    nc = await nats.connect(
        settings.nats_url, connect_timeout=5, max_reconnect_attempts=-1
    )
    log.info("connected to NATS at %s", settings.nats_url)

    runner = _build_runner(settings)
    agent = Agent(
        instance_id=settings.instance_id,
        nats_client=nc,
        compose_runner=runner,
    )
    await agent.start()
    log.info(
        "subscribed; waiting for commands on commands.%s", settings.instance_id
    )

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await agent.stop()
        await nc.drain()


def cli_main() -> None:
    settings = AgentSettings()
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli_main()

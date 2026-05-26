from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import httpx
import nats

from axis_agent.agent import Agent
from axis_agent.compose_runner import ComposeRunner
from axis_agent.config import AgentSettings
from axis_agent.control_plane import ControlPlaneClient
from axis_agent.identity import AgentIdentityStore
from axis_agent.registration import (
    RegistrationFailed,
    RegistrationInputs,
    ensure_identity,
)
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


async def _resolve_identity(settings: AgentSettings) -> AgentIdentityStore | None:
    """Returns the store so the caller can act on it; instance_id is read
    from `settings.instance_id` afterwards (mutated in place)."""

    store = AgentIdentityStore(state_dir=settings.state_dir)
    inputs = RegistrationInputs(
        project_name=settings.project_name,
        hostname=settings.hostname,
        override_instance_id=settings.instance_id,
        max_attempts=settings.register_max_attempts,
        initial_backoff=settings.register_initial_backoff,
        max_backoff=settings.register_max_backoff,
    )
    async with httpx.AsyncClient(
        base_url=settings.control_plane_url, timeout=10.0
    ) as http:
        client = ControlPlaneClient(http=http)
        resolved = await ensure_identity(
            inputs=inputs, store=store, client=client
        )
    settings.instance_id = resolved
    return store


async def _run(settings: AgentSettings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info(
        "axis-agent starting project=%s hostname=%s mode=%s",
        settings.project_name,
        settings.hostname,
        settings.compose_mode,
    )

    await _resolve_identity(settings)
    assert settings.instance_id is not None  # set by _resolve_identity

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="axis-agent")
    parser.add_argument(
        "--reset-identity",
        action="store_true",
        help="Delete the persisted instance.json before starting, forcing "
        "the agent to re-register with the control plane.",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = AgentSettings()
    if args.reset_identity:
        AgentIdentityStore(state_dir=settings.state_dir).clear()
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        return 0
    except RegistrationFailed as exc:
        log.error("registration failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())

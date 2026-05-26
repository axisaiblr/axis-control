from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class LoggingComposeRunner:
    """Dry-run runner: logs what *would* happen, never touches docker.

    Default for local development so issuing a disable from the admin API
    cannot accidentally stop a real workload on the dev machine.
    """

    async def stop(self) -> None:
        log.info("[dry-run] would: docker compose stop")

    async def start(self) -> None:
        log.info("[dry-run] would: docker compose start")


class DockerComposeRunner:
    """Real runner: shells out to `docker compose -f <file> [-p <project>]`.

    Selected via AXIS_AGENT_COMPOSE_MODE=docker. Requires the host docker
    socket to be reachable (mount `/var/run/docker.sock` if the agent is
    itself containerised).
    """

    def __init__(
        self,
        compose_file: Path,
        project_name: str | None = None,
    ) -> None:
        self._file = Path(compose_file)
        self._project = project_name

    async def stop(self) -> None:
        await self._run("stop")

    async def start(self) -> None:
        await self._run("start")

    async def _run(self, verb: str) -> None:
        cmd: list[str] = ["docker", "compose", "-f", str(self._file)]
        if self._project:
            cmd += ["-p", self._project]
        cmd.append(verb)

        log.info("running: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker compose {verb} exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )
        if stdout:
            log.debug("docker compose %s stdout: %s", verb, stdout.decode())

"""Tests that exercise the published agent OCI image.

Same shape as packages/control/tests/test_docker_image.py — see that
file for the rationale. The agent image is special in one extra way:
the runtime stage must carry a working Docker CLI + compose plugin,
because `axis-agent` shells out to `docker compose` against the
worker host's daemon (mounted at /var/run/docker.sock).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "packages" / "agent" / "Dockerfile"
IMAGE_TAG = "axis-agent:pytest"

pytestmark = pytest.mark.docker_image


@pytest.fixture(scope="session")
def agent_image() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available on host")
    if not DOCKERFILE.exists():
        pytest.fail(f"missing Dockerfile at {DOCKERFILE}")
    proc = subprocess.run(
        [
            "docker", "build",
            "-f", str(DOCKERFILE),
            "-t", IMAGE_TAG,
            str(REPO_ROOT),
        ],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.fail(
            "docker build failed:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return IMAGE_TAG


def test_axis_agent_is_on_path(agent_image: str) -> None:
    """Console script must be installed in the runtime venv on $PATH."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--entrypoint", "/bin/sh",
            agent_image,
            "-c", "command -v axis-agent",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"axis-agent not on PATH inside image\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert proc.stdout.strip().endswith("axis-agent"), proc.stdout


def test_entrypoint_is_axis_agent(agent_image: str) -> None:
    """`docker run <image>` must invoke axis-agent by default."""
    proc = subprocess.run(
        [
            "docker", "inspect",
            "--format", "{{json .Config.Entrypoint}}",
            agent_image,
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    entrypoint = json.loads(proc.stdout.strip())
    assert entrypoint == ["axis-agent"], entrypoint


def test_docker_cli_is_installed(agent_image: str) -> None:
    """The agent shells out to `docker compose ...` (see DockerComposeRunner).
    Both the CLI itself and the v2 compose subcommand must be available
    inside the runtime stage — otherwise compose actions fail at runtime
    with FileNotFoundError, not a recognisable error."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--entrypoint", "/bin/sh",
            agent_image,
            "-c", "docker --version && docker compose version",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"docker CLI / compose plugin missing from agent image\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "Docker version" in proc.stdout, proc.stdout
    assert "Docker Compose" in proc.stdout, proc.stdout

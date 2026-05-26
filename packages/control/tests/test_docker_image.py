"""Tests that exercise the published control-plane OCI image.

These tests perform a real `docker build` against the repo root and then
run short, side-effect-free commands inside the image. The build is
session-scoped so the cost is paid once; subsequent runs reuse the
layer cache.

Deselect with `-m 'not docker_image'` during fast iteration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "packages" / "control" / "Dockerfile"
IMAGE_TAG = "axis-control:pytest"

pytestmark = pytest.mark.docker_image


@pytest.fixture(scope="session")
def control_image() -> str:
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


def test_axis_control_is_on_path(control_image: str) -> None:
    """The console script defined in pyproject.toml must be installed
    and discoverable on $PATH inside the runtime stage. Failure here
    means the venv/PATH wiring in the Dockerfile is broken."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--entrypoint", "/bin/sh",
            control_image,
            "-c", "command -v axis-control",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"axis-control not on PATH inside image\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert proc.stdout.strip().endswith("axis-control"), proc.stdout


def test_entrypoint_is_axis_control(control_image: str) -> None:
    """`docker run <image>` (no args) must invoke axis-control. Pins
    the ENTRYPOINT directive so a refactor that drops it surfaces here
    instead of in production."""
    proc = subprocess.run(
        [
            "docker", "inspect",
            "--format", "{{json .Config.Entrypoint}}",
            control_image,
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    entrypoint = json.loads(proc.stdout.strip())
    assert entrypoint == ["axis-control"], entrypoint

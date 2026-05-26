"""Tests for the worker docker-compose.yml template that operators
drop in next to their project's own compose file.

The template ships only the `axis-agent` sidecar — the project's own
compose (text-assistant, voice-assistant, …) lives in the project repo
and is invoked alongside. The agent shells out to that sibling compose
via the host docker socket.

These are all static checks: they shell out to `docker compose config`
to parse the file with a sample env, then assert structural invariants
about the rendered config. Fast: only needs the Docker CLI on PATH; the
daemon does not have to pull or run anything.

A worker-side integration test (agent registers, heartbeats, executes
a command) needs NATS exposed to remote workers, which blocks on the
auth story in #8; deferred.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_COMPOSE_FILE = REPO_ROOT / "docker-compose.worker.yml"

pytestmark = pytest.mark.worker_compose


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available on host")


def _sample_env(tmp_path: Path) -> Path:
    """Minimal env file that satisfies every required interpolation in
    the worker template. Distinct from the repo's .env.example so a test
    run never depends on operator edits to it.

    AXIS_AGENT_COMPOSE_FILE points at a path on the test host that is
    guaranteed to exist (the production compose file in the repo). The
    template bind-mounts whatever the operator sets, so the path just
    needs to exist for the rendered config to be valid; what's at the
    path is irrelevant to a static check.
    """
    env = tmp_path / "worker.env"
    env.write_text(
        "\n".join(
            [
                "AXIS_AGENT_PROJECT_NAME=text-assistant",
                "AXIS_AGENT_CONTROL_PLANE_URL=https://admin.example.com",
                "AXIS_AGENT_NATS_URL=nats://admin.example.com:4222",
                "AXIS_AGENT_REGISTRATION_TOKEN=test-bootstrap-secret",
                f"AXIS_AGENT_COMPOSE_FILE={REPO_ROOT / 'docker-compose.yml'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env


def _compose_config(env_file: Path) -> dict:
    """Run `docker compose config` and return the rendered YAML as dict."""
    _require_docker()
    proc = subprocess.run(
        [
            "docker", "compose",
            "-f", str(WORKER_COMPOSE_FILE),
            "--env-file", str(env_file),
            "config",
        ],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "`docker compose config` failed:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return yaml.safe_load(proc.stdout)


def test_worker_compose_config_parses(tmp_path: Path) -> None:
    """Tracer bullet: docker can parse docker-compose.worker.yml with a
    sample env. Catches typos, missing interpolated vars, and bad
    references before any structural assertion runs."""
    assert WORKER_COMPOSE_FILE.exists(), f"missing {WORKER_COMPOSE_FILE}"
    config = _compose_config(_sample_env(tmp_path))
    assert isinstance(config, dict)
    assert "services" in config and config["services"], (
        "worker compose file declared no services"
    )


def _service(config: dict, name: str) -> dict:
    services = config["services"]
    assert name in services, f"service {name!r} not declared in compose"
    return services[name]


def _published_host_ports(service: dict) -> set[int]:
    """Return host-side ports a service publishes."""
    published: set[int] = set()
    for entry in service.get("ports", []):
        if isinstance(entry, dict):
            value = entry.get("published")
            if value is not None:
                published.add(int(value))
        elif isinstance(entry, str):
            parts = entry.split(":")
            published.add(int(parts[-2]) if len(parts) >= 2 else int(parts[0]))
    return published


def test_axis_agent_uses_ghcr_image_with_tag_override(tmp_path: Path) -> None:
    """Agent pulls from the same GHCR repo the publish workflow pushes to,
    with an env-overridable tag (operator pins to a specific version on
    each worker VPS; `:latest` is the convenient default for staging).
    Outbound-only — no host port published, since the agent only opens
    outbound NATS + HTTP connections to the control plane."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")

    assert agent["image"] == "ghcr.io/axisaiblr/axis-agent:latest", (
        f"agent image must default to GHCR :latest, got {agent['image']!r}"
    )
    assert agent.get("restart") == "unless-stopped", (
        "agent must restart on crash — worker has no operator on call"
    )
    assert _published_host_ports(agent) == set(), (
        f"agent must not publish host ports (outbound-only), got "
        f"{_published_host_ports(agent)}"
    )


def test_axis_agent_image_tag_is_env_overridable(tmp_path: Path) -> None:
    """Operator must be able to pin the agent image to a specific tag via
    AXIS_AGENT_IMAGE_TAG. Without this an upgrade requires editing the
    compose file in place on every worker VPS."""
    env = tmp_path / "tag-override.env"
    env.write_text(
        "\n".join(
            [
                "AXIS_AGENT_PROJECT_NAME=text-assistant",
                "AXIS_AGENT_CONTROL_PLANE_URL=https://admin.example.com",
                "AXIS_AGENT_NATS_URL=nats://admin.example.com:4222",
                "AXIS_AGENT_REGISTRATION_TOKEN=test-bootstrap-secret",
                f"AXIS_AGENT_COMPOSE_FILE={REPO_ROOT / 'docker-compose.yml'}",
                "AXIS_AGENT_IMAGE_TAG=1.2.3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = _compose_config(env)
    agent = _service(config, "axis-agent")
    assert agent["image"] == "ghcr.io/axisaiblr/axis-agent:1.2.3", agent["image"]


def _env_dict(service: dict) -> dict[str, str]:
    """`docker compose config` normalises environment to a dict or a
    list of KEY=VALUE strings depending on input shape; accept both."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return dict(env)


def test_required_agent_env_is_wired_through(tmp_path: Path) -> None:
    """The operator-facing config story: set four env vars on the host
    (project name, control plane URL, NATS URL, sibling compose path)
    and the agent picks them all up. Any of these missing means the
    agent either fails to register, can't reach the broker, or runs in
    dry-run mode against a production worker."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")
    env = _env_dict(agent)

    assert env.get("AXIS_AGENT_PROJECT_NAME") == "text-assistant", env
    assert env.get("AXIS_AGENT_CONTROL_PLANE_URL") == "https://admin.example.com", env
    assert env.get("AXIS_AGENT_NATS_URL") == "nats://admin.example.com:4222", env
    assert env.get("AXIS_AGENT_COMPOSE_FILE") == str(REPO_ROOT / "docker-compose.yml"), env
    # Bootstrap secret for self-registration (#8). Mismatch with the
    # control plane → agent fails to register and exits.
    assert env.get("AXIS_AGENT_REGISTRATION_TOKEN") == "test-bootstrap-secret", env


def test_compose_mode_defaults_to_docker_not_logging(tmp_path: Path) -> None:
    """The `logging` runner is a dev-time dry-run that never actually
    stops a container — running it on a real worker would silently drop
    every disable/enable command. The worker template must flip
    AXIS_AGENT_COMPOSE_MODE to `docker` by default."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")
    env = _env_dict(agent)
    assert env.get("AXIS_AGENT_COMPOSE_MODE") == "docker", (
        f"worker template defaults compose mode to {env.get('AXIS_AGENT_COMPOSE_MODE')!r}; "
        "must be 'docker' or commands silently no-op on production workers"
    )


def _volume_entries(service: dict) -> list[dict]:
    """Return normalised volume entries (`source`/`target`/`type`) for a
    service. `docker compose config` may emit either the long form
    (dict) or the short form ("src:dst[:mode]"); smooth over both."""
    out: list[dict] = []
    for entry in service.get("volumes", []):
        if isinstance(entry, dict):
            out.append(entry)
        elif isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2:
                out.append({"source": parts[0], "target": parts[1]})
    return out


def test_docker_socket_bind_mounted_into_agent(tmp_path: Path) -> None:
    """The agent shells out to `docker compose stop/start` against the
    sibling project's compose file. That CLI talks to the host docker
    daemon over /var/run/docker.sock — without this mount,
    AXIS_AGENT_COMPOSE_MODE=docker fails immediately on the first
    command with `Cannot connect to the Docker daemon`."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")

    sock_mounts = [
        e for e in _volume_entries(agent)
        if e.get("target") == "/var/run/docker.sock"
    ]
    assert sock_mounts, (
        "agent must bind-mount /var/run/docker.sock — without it the "
        "agent cannot drive `docker compose` against the worker host"
    )
    assert sock_mounts[0].get("source") == "/var/run/docker.sock", (
        f"docker socket must come from the host /var/run/docker.sock, "
        f"got {sock_mounts[0].get('source')!r}"
    )


def test_project_compose_file_passes_through_at_same_path(tmp_path: Path) -> None:
    """The agent runs `docker compose -f $AXIS_AGENT_COMPOSE_FILE
    stop/start`. That command runs inside the agent container, but the
    daemon it talks to lives on the host — so any host paths embedded
    in the compose file (volumes, build contexts) must resolve the
    same way on both sides. The template solves this by bind-mounting
    the host path the operator sets into the agent at the *same* path,
    read-only. The agent reads the file; the daemon resolves any host
    paths in it natively."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")

    host_path = str(REPO_ROOT / "docker-compose.yml")
    passthroughs = [
        e for e in _volume_entries(agent)
        if e.get("source") == host_path and e.get("target") == host_path
    ]
    assert passthroughs, (
        f"agent must bind-mount the project compose at the same path "
        f"on host and in container; expected source==target=={host_path!r}, "
        f"got mounts {_volume_entries(agent)!r}"
    )
    # `docker compose config` emits `read_only: true` as a top-level key
    # on the long-form entry. The agent only ever reads the sibling
    # compose file — write access would let a bug clobber the project's
    # own deploy spec.
    assert passthroughs[0].get("read_only") is True, (
        f"project compose mount must be read-only, got {passthroughs[0]!r}"
    )


def test_agent_state_persists_on_named_volume(tmp_path: Path) -> None:
    """instance.json (the assigned UUID + the agent's identity) must
    survive `docker compose up` cycles — otherwise every restart of the
    worker stack triggers a fresh self-registration and the operator
    sees the same physical worker pile up as a new row each time.

    A named volume keeps the file across recreates and is independent
    of any host directory (so the operator picks no path). The agent
    container reads AXIS_AGENT_STATE_DIR off the env, which the
    template pins to the volume mount target."""
    config = _compose_config(_sample_env(tmp_path))
    agent = _service(config, "axis-agent")
    env = _env_dict(agent)

    state_dir = env.get("AXIS_AGENT_STATE_DIR")
    assert state_dir, (
        "agent template must set AXIS_AGENT_STATE_DIR to a known path so "
        "the persistent volume target is unambiguous"
    )

    volumes = _volume_entries(agent)
    state_mount = next(
        (e for e in volumes if e.get("target") == state_dir), None,
    )
    assert state_mount, (
        f"agent state dir {state_dir!r} is not mounted; instance.json "
        f"will not survive `docker compose up`. Mounts: {volumes!r}"
    )

    # Source must be a *named volume*, not a bind mount. Bind mounts
    # force the operator to pick a host path, defeating the goal of a
    # drop-in template.
    assert state_mount.get("type") == "volume", (
        f"agent state mount must be a named volume (type=volume); got "
        f"{state_mount.get('type')!r}. A bind mount would require the "
        f"operator to commit to a host path before deploying."
    )

    declared_volumes = (config.get("volumes") or {})
    src = state_mount.get("source")
    assert src in declared_volumes, (
        f"named volume {src!r} is not declared in the top-level "
        f"`volumes:` block; got {sorted(declared_volumes.keys())}"
    )


def test_declares_only_the_axis_agent_service(tmp_path: Path) -> None:
    """The worker template ships *only* the agent sidecar — the project's
    own compose lives in the project repo. An unexpected extra service
    here means the template has started colonising space that belongs to
    the project, which couples our release cadence to theirs."""
    config = _compose_config(_sample_env(tmp_path))
    declared = set(config["services"].keys())
    assert declared == {"axis-agent"}, (
        f"worker template should only declare axis-agent; got {sorted(declared)}"
    )

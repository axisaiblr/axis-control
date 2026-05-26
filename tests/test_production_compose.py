"""Tests for the production docker-compose.yml that runs on the
management VPS.

These tests are layered:

* `production_compose` (default-on) — static checks. They shell out to
  `docker compose config` to parse the file, then assert structural
  invariants about the rendered config. Fast: only needs the Docker
  CLI on PATH; the daemon does not have to pull or run anything.

* `production_compose_integration` (slow) — bring the full stack up,
  exercise it through caddy, tear down. Pulls real images and needs
  a working Docker daemon. Deselect with `-m 'not
  production_compose_integration'` during fast iteration.

The intent is that the static layer catches typos / contract drift
on every test run, while the integration layer runs in CI and before
a release to confirm the stack actually boots.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONTROL_DOCKERFILE = REPO_ROOT / "packages" / "control" / "Dockerfile"
INTEGRATION_IMAGE_TAG = "integration-test"
INTEGRATION_FULL_IMAGE = f"ghcr.io/axisaiblr/axis-control:{INTEGRATION_IMAGE_TAG}"

# Default marker for everything in this file. Individual integration
# tests opt in to the heavier marker as well.
pytestmark = pytest.mark.production_compose


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available on host")


def _compose_config() -> dict:
    """Run `docker compose config` and return the rendered YAML as dict."""
    _require_docker()
    proc = subprocess.run(
        [
            "docker", "compose",
            "-f", str(COMPOSE_FILE),
            "--env-file", str(ENV_EXAMPLE),
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


EXPECTED_SERVICES = {
    "caddy",
    "postgres",
    "nats",
    "axis-control",
    "vmsingle",
    "grafana",
}


def test_compose_config_parses() -> None:
    """Tracer bullet: docker can parse docker-compose.yml with .env.example
    as the env file. Catches typos, missing interpolated vars, and
    bad references before anything else."""
    assert COMPOSE_FILE.exists(), f"missing {COMPOSE_FILE}"
    assert ENV_EXAMPLE.exists(), f"missing {ENV_EXAMPLE}"
    config = _compose_config()
    assert isinstance(config, dict)
    assert "services" in config and config["services"], (
        "compose file declared no services"
    )


def _service(config: dict, name: str) -> dict:
    services = config["services"]
    assert name in services, f"service {name!r} not declared in compose"
    return services[name]


def _volume_targets(service: dict) -> set[str]:
    """Return the in-container mount paths declared on a service.

    `docker compose config` normalises every `volumes:` entry into a
    dict with `source` / `target`; this helper smooths over the short-
    form vs long-form difference."""
    targets: set[str] = set()
    for entry in service.get("volumes", []):
        if isinstance(entry, dict):
            targets.add(entry["target"])
        elif isinstance(entry, str):
            # short form "source:target[:mode]"
            targets.add(entry.split(":")[1])
    return targets


def _published_host_ports(service: dict) -> set[int]:
    """Return host-side ports a service publishes."""
    published: set[int] = set()
    for entry in service.get("ports", []):
        if isinstance(entry, dict):
            # long form: {published: 80, target: 80, ...}
            value = entry.get("published")
            if value is not None:
                published.add(int(value))
        elif isinstance(entry, str):
            # short form: "80:80" / "127.0.0.1:80:80" / "80"
            parts = entry.split(":")
            published.add(int(parts[-2]) if len(parts) >= 2 else int(parts[0]))
    return published


def _depends_on(service: dict) -> dict[str, dict]:
    """Normalise depends_on (short list or long dict form) to the dict form."""
    raw = service.get("depends_on")
    if raw is None:
        return {}
    if isinstance(raw, list):
        return {name: {"condition": "service_started"} for name in raw}
    return dict(raw)


def test_grafana_admin_password_from_env_and_persistent_volume() -> None:
    """Grafana's admin password must come from .env (never hardcoded;
    never the default `admin` Grafana now refuses). Dashboards and
    user-edited state live on a persistent volume so the operator does
    not redo their work after each restart. Depends on vmsingle so the
    datasource is reachable when Grafana boots."""
    config = _compose_config()
    grafana = _service(config, "grafana")

    assert grafana["image"].startswith("grafana/grafana:"), grafana["image"]
    assert grafana.get("restart") == "unless-stopped"

    env = grafana.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    admin_pw = env.get("GF_SECURITY_ADMIN_PASSWORD")
    assert admin_pw, (
        "grafana GF_SECURITY_ADMIN_PASSWORD must be set from the env"
    )
    assert admin_pw not in ("admin", ""), (
        f"grafana admin password is the default placeholder {admin_pw!r}"
    )

    assert "/var/lib/grafana" in _volume_targets(grafana), (
        "grafana storage is not on a persistent volume"
    )

    deps = _depends_on(grafana)
    assert "vmsingle" in deps, "grafana must depends_on vmsingle"


def test_vmsingle_has_persistent_volume() -> None:
    """VictoriaMetrics single-node stores scrape data on a named volume
    — losing the metric history on a stack restart would defeat the
    point of having long-running dashboards. Not reachable from the
    host directly; Grafana proxies queries on the docker network."""
    config = _compose_config()
    vm = _service(config, "vmsingle")

    assert vm["image"].startswith("victoriametrics/victoria-metrics:"), (
        vm["image"]
    )
    assert vm.get("restart") == "unless-stopped"
    assert "/victoria-metrics-data" in _volume_targets(vm), (
        "vmsingle storage path /victoria-metrics-data is not on a "
        "persistent volume — stack restart would wipe scrape history"
    )
    assert _published_host_ports(vm) == set(), (
        "vmsingle should be reachable only inside the docker network"
    )


def test_caddy_publishes_80_443_with_persistent_acme_state() -> None:
    """Caddy is the only inbound port on the VPS. It publishes :80
    (HTTP + Let's Encrypt challenges) and :443 (HTTPS). The Caddyfile
    is bind-mounted so an operator can edit it without rebuilding an
    image, and the ACME state lives on named volumes so a stack
    restart does not re-request certificates (Let's Encrypt rate-
    limits aggressively)."""
    config = _compose_config()
    caddy = _service(config, "caddy")

    assert caddy["image"].startswith("caddy:"), caddy["image"]
    assert caddy.get("restart") == "unless-stopped"
    assert _published_host_ports(caddy) >= {80, 443}, (
        f"caddy must publish :80 and :443, got {_published_host_ports(caddy)}"
    )
    targets = _volume_targets(caddy)
    assert "/etc/caddy/Caddyfile" in targets, (
        "Caddyfile is not bind-mounted; an operator cannot edit routing "
        "without rebuilding"
    )
    assert "/data" in targets, (
        "caddy /data (ACME state) is not on a persistent volume — a "
        "restart will re-issue certs and may hit Let's Encrypt rate limits"
    )
    assert "/config" in targets, (
        "caddy /config (autosave) is not on a persistent volume"
    )


def test_axis_control_wired_to_postgres_and_nats() -> None:
    """axis-control runs from the GHCR image, talks to postgres and
    nats over the internal docker network (service DNS), and is not
    directly reachable from the host — caddy fronts it. It must wait
    for postgres to be healthy before starting so the schema apply on
    boot does not race the postgres init."""
    config = _compose_config()
    control = _service(config, "axis-control")

    assert control["image"].startswith("ghcr.io/axisaiblr/axis-control"), (
        control["image"]
    )
    assert control.get("restart") == "unless-stopped"
    assert _published_host_ports(control) == set(), (
        "axis-control should be reachable only via caddy, not the host"
    )

    env = control.get("environment") or {}
    # `docker compose config` normalises environment to a dict or to a
    # KEY=VALUE list; accept either.
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    assert "postgres" in env.get("AXIS_CONTROL_DATABASE_URL", ""), (
        "axis-control DATABASE_URL must point at the postgres service "
        f"by name, got {env.get('AXIS_CONTROL_DATABASE_URL')!r}"
    )
    assert "nats" in env.get("AXIS_CONTROL_NATS_URL", ""), (
        "axis-control NATS_URL must point at the nats service by name, "
        f"got {env.get('AXIS_CONTROL_NATS_URL')!r}"
    )
    # Bootstrap secret for the registration endpoint (#8). Without it
    # POST /api/instances refuses every request, so the management
    # stack must thread it through from the host .env.
    assert env.get("AXIS_CONTROL_REGISTRATION_TOKEN"), (
        "axis-control must receive AXIS_CONTROL_REGISTRATION_TOKEN from "
        "the host .env — without it agents cannot self-register"
    )

    deps = _depends_on(control)
    assert "postgres" in deps, "axis-control must depends_on postgres"
    assert deps["postgres"].get("condition") == "service_healthy", (
        "axis-control must wait for postgres to be healthy, not just "
        "started — schema apply will race a slow init otherwise"
    )
    assert "nats" in deps, "axis-control must depends_on nats"


def test_nats_is_internal_only_with_restart_policy() -> None:
    """NATS is reachable to the in-network axis-control service and to
    nothing else: the broker has no auth in v0.1 (see #8), so exposing
    the client port to the host would let anything on the VPS publish
    commands. Restart=unless-stopped so a single broker crash does not
    require operator intervention."""
    config = _compose_config()
    nats = _service(config, "nats")

    assert nats["image"].startswith("nats:"), nats["image"]
    assert nats.get("restart") == "unless-stopped"
    assert _published_host_ports(nats) == set(), (
        "nats must not publish a host port until auth lands (#8)"
    )


def test_postgres_has_persistent_volume_and_healthcheck() -> None:
    """Postgres data must live on a named volume (so a `docker compose
    down && up` does not wipe the row history) and must expose a
    healthcheck (so axis-control's `depends_on: service_healthy` has
    something to wait on)."""
    config = _compose_config()
    postgres = _service(config, "postgres")

    assert postgres["image"].startswith("postgres:"), postgres["image"]
    assert postgres.get("restart") == "unless-stopped"
    assert "/var/lib/postgresql/data" in _volume_targets(postgres), (
        "postgres data dir is not on a persistent volume"
    )
    assert "healthcheck" in postgres and postgres["healthcheck"].get("test"), (
        "postgres missing a healthcheck"
    )
    # Database lives inside the docker network only — exposing 5432 to
    # the host is a foot-gun on a multi-tenant VPS. If a future feature
    # needs it, expose it deliberately.
    assert _published_host_ports(postgres) == set(), (
        "postgres should not publish a host port in production"
    )


def test_declares_expected_services() -> None:
    """The management plane is six services. Any drift here is a
    behaviour change worth seeing in a diff: a missing service means
    deploys will lose a capability; an unexpected service means we're
    shipping something nobody designed for."""
    config = _compose_config()
    declared = set(config["services"].keys())
    assert declared == EXPECTED_SERVICES, (
        f"unexpected service set\n"
        f"  expected: {sorted(EXPECTED_SERVICES)}\n"
        f"  declared: {sorted(declared)}\n"
        f"  missing:  {sorted(EXPECTED_SERVICES - declared)}\n"
        f"  extra:    {sorted(declared - EXPECTED_SERVICES)}"
    )


# =========================================================================
# Integration layer — actually bring the stack up.
#
# These tests need a working Docker daemon and pull real images. The
# axis-control image is built locally from packages/control/Dockerfile
# rather than pulled from GHCR, so the test does not depend on a tag
# being published. All other images come from public registries.
# =========================================================================

INTEGRATION_MARKERS = (
    pytest.mark.production_compose,
    pytest.mark.production_compose_integration,
)


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _free_host_port() -> int:
    """Bind to a kernel-assigned free port, release it, and hand the
    number back. There is a tiny race between release and re-bind by
    docker; on a workstation that is not running other servers it has
    never come up in practice."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def axis_control_image() -> str:
    """Build the control-plane image locally and tag it as the value the
    production compose will look for when AXIS_CONTROL_IMAGE_TAG is set
    to `integration-test`. Session-scoped so the cost is paid once."""
    if not _docker_daemon_available():
        pytest.skip("docker daemon not reachable")
    if not CONTROL_DOCKERFILE.exists():
        pytest.fail(f"missing {CONTROL_DOCKERFILE}")
    proc = subprocess.run(
        [
            "docker", "build",
            "-f", str(CONTROL_DOCKERFILE),
            "-t", INTEGRATION_FULL_IMAGE,
            str(REPO_ROOT),
        ],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.fail(
            "axis-control image build failed:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return INTEGRATION_FULL_IMAGE


def _write_integration_env(tmpdir: Path, caddy_host_port: int) -> Path:
    """An .env file that satisfies every required interpolation in the
    production compose. Distinct from the repo's .env.example so a
    test run never depends on operator edits to it."""
    env = tmpdir / "env"
    env.write_text(
        "\n".join(
            [
                "POSTGRES_USER=axis",
                "POSTGRES_PASSWORD=integration-pw",
                "POSTGRES_DB=axis",
                "GRAFANA_ADMIN_PASSWORD=integration-pw",
                f"AXIS_CONTROL_IMAGE_TAG={INTEGRATION_IMAGE_TAG}",
                # `http://` scheme opts Caddy out of auto-HTTPS — there
                # is no ACME-issuable cert for `localhost` and we don't
                # want the redirect-to-https. Production VPS sets a real
                # domain with no scheme so Caddy negotiates Let's Encrypt
                # automatically.
                "ADMIN_DOMAIN=http://localhost",
                f"CADDY_HOST_PORT={caddy_host_port}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env


def _write_integration_override(tmpdir: Path) -> Path:
    """An override that:
      * publishes caddy on a kernel-chosen high port (not 80/443),
        so the test does not collide with anything on the host and
        does not need root.
      * sets pull_policy=never on axis-control so docker compose uses
        the locally-tagged image instead of trying to GET it from GHCR.
    """
    override = tmpdir / "docker-compose.override.yml"
    override.write_text(
        """\
services:
  caddy:
    ports: !override
      - "127.0.0.1:${CADDY_HOST_PORT}:80"
  axis-control:
    pull_policy: never
""",
        encoding="utf-8",
    )
    return override


class _Stack:
    """Wraps a `docker compose` invocation under one unique project name.
    Cleaning up means `down --volumes`; the project-name namespacing
    keeps parallel test runs from stepping on each other."""

    def __init__(
        self,
        project: str,
        env_file: Path,
        override_file: Path,
        caddy_host_port: int,
    ) -> None:
        self.project = project
        self.env_file = env_file
        self.override_file = override_file
        self.caddy_host_port = caddy_host_port

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            "docker", "compose",
            "-p", self.project,
            "-f", str(COMPOSE_FILE),
            "-f", str(self.override_file),
            "--env-file", str(self.env_file),
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=900, check=check,
            cwd=str(REPO_ROOT),
        )

    def up(self) -> None:
        proc = self._compose("up", "-d", "--wait", "--wait-timeout", "180",
                             check=False)
        if proc.returncode != 0:
            logs = self._compose("logs", "--no-color", check=False).stdout
            pytest.fail(
                "docker compose up failed:\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}\n"
                f"--- service logs ---\n{logs}"
            )

    def down(self, remove_volumes: bool) -> None:
        args = ["down", "--remove-orphans"]
        if remove_volumes:
            args.append("--volumes")
        self._compose(*args, check=False)


@pytest.fixture
def integration_stack(
    axis_control_image: str,
    tmp_path: Path,
) -> Iterator[_Stack]:
    """One stack per test. Slow but isolated — failures in one test do
    not leak state into the next."""
    project = f"axis_prod_it_{uuid.uuid4().hex[:8]}"
    caddy_port = _free_host_port()
    env_file = _write_integration_env(tmp_path, caddy_port)
    override_file = _write_integration_override(tmp_path)
    stack = _Stack(project, env_file, override_file, caddy_port)
    try:
        stack.up()
        yield stack
    finally:
        stack.down(remove_volumes=True)


def _retry_get(
    url: str,
    host_header: str,
    timeout: float = 90.0,
    interval: float = 1.5,
) -> httpx.Response:
    """Poll `url` (with `Host: host_header`) until it returns 2xx or
    `timeout` elapses. Caddy's site blocks match on Host, so the test
    connects to 127.0.0.1 but advertises the configured admin domain
    — the same routing path real clients exercise.

    The error surfaced on timeout names what actually went wrong
    (connection refused vs 5xx vs unmatched-vhost empty 200) so a
    failure is debuggable from the assertion message alone."""
    deadline = time.monotonic() + timeout
    last_err: str = "no attempt made"
    headers = {"Host": host_header}
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, headers=headers, timeout=3.0)
            if 200 <= r.status_code < 300 and r.content:
                return r
            if 200 <= r.status_code < 300:
                last_err = (
                    f"HTTP {r.status_code} with empty body — Caddy "
                    "likely fell through to the catch-all (Host header "
                    "didn't match a site block)"
                )
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    pytest.fail(f"GET {url} never succeeded within {timeout}s. Last: {last_err}")


@pytest.mark.production_compose_integration
def test_volumes_persist_across_stack_restart(
    axis_control_image: str, tmp_path: Path
) -> None:
    """Postgres' data volume is named (not anonymous), so a `docker
    compose down && up` keeps every row the operator created. This
    test takes the most observable path possible: register an
    instance through the API, take the stack down (volumes intact),
    bring it back up, and GET the same instance back.

    Catches three real failure modes the static tests can't see:
      * volume is anonymous → row is gone after recreate.
      * schema apply on startup is not idempotent → second up crashes.
      * caddy data volume not persisted → cert state is lost (not
        asserted here directly, but the same `down && up` exercises it)."""
    project = f"axis_prod_persist_{uuid.uuid4().hex[:8]}"
    caddy_port = _free_host_port()
    env_file = _write_integration_env(tmp_path, caddy_port)
    override_file = _write_integration_override(tmp_path)
    stack = _Stack(project, env_file, override_file, caddy_port)
    base_url = f"http://127.0.0.1:{caddy_port}"
    host_header = "localhost"

    try:
        stack.up()
        # 1. Register an instance via the API as a real operator would.
        # Retry on POST until the app is up — same pattern as the
        # healthz smoke test.
        deadline = time.monotonic() + 90
        instance_id: str | None = None
        last_err = "no attempt"
        while time.monotonic() < deadline and instance_id is None:
            try:
                r = httpx.post(
                    f"{base_url}/api/instances",
                    json={
                        "project_name": "persistence-probe",
                        "hostname": "persistence-host-1",
                    },
                    headers={"Host": host_header},
                    timeout=5.0,
                )
                if r.status_code == 201:
                    instance_id = r.json()["id"]
                else:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if instance_id is None:
                time.sleep(1.5)
        assert instance_id, f"register never succeeded; last={last_err}"

        # 2. Stop the stack but keep volumes — the operator's down/up.
        stack.down(remove_volumes=False)

        # 3. Bring it back up against the same volumes.
        stack.up()

        # 4. The same instance must still be there after the restart.
        r = _retry_get(
            f"{base_url}/api/instances/{instance_id}",
            host_header=host_header,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == instance_id, body
        assert body["hostname"] == "persistence-host-1", body
        assert body["project_name"] == "persistence-probe", body
    finally:
        stack.down(remove_volumes=True)


@pytest.mark.production_compose_integration
def test_caddy_routes_healthz_to_axis_control(integration_stack: _Stack) -> None:
    """End-to-end smoke: the management plane comes up, caddy reverse-
    proxies the configured admin domain to axis-control, and the
    control-plane healthcheck endpoint is reachable from the host —
    proving the full ingress path works as deployed."""
    url = f"http://127.0.0.1:{integration_stack.caddy_host_port}/healthz"
    response = _retry_get(url, host_header="localhost")
    assert response.json() == {"status": "ok"}, response.text

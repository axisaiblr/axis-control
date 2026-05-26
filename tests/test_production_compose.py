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
BACKUP_DOCKERFILE = REPO_ROOT / "packages" / "backup" / "Dockerfile"
INTEGRATION_IMAGE_TAG = "integration-test"
INTEGRATION_FULL_IMAGE = f"ghcr.io/axisaiblr/axis-control:{INTEGRATION_IMAGE_TAG}"
INTEGRATION_BACKUP_IMAGE = f"ghcr.io/axisaiblr/axis-backup:{INTEGRATION_IMAGE_TAG}"

# Basicauth credentials wired into the integration stack. The hash was
# generated once with
#   docker run --rm caddy:2-alpine caddy hash-password --plaintext test-pw
# bcrypt is non-deterministic (cost-14 salt) so any valid hash for the
# same plaintext works; this one is checked in for reproducibility.
INTEGRATION_BASICAUTH_USER = "operator"
INTEGRATION_BASICAUTH_PASSWORD = "test-pw"
INTEGRATION_BASICAUTH_HASH = (
    "$2a$14$AOGr9G.Nxov9UY0JcF..eeJzFE/EvqvqGxMXdZcZ7WrXn2BZx.ahi"
)

# Worker basicauth (#26) — separate audience and rotation cadence
# from the operator basicauth above. Hash generated once with
#   docker run --rm caddy:2-alpine caddy hash-password --plaintext worker-pw
# and checked in for reproducibility (bcrypt's salt is non-deterministic
# so any hash that round-trips against the plaintext works).
INTEGRATION_WORKER_BASICAUTH_USER = "worker"
INTEGRATION_WORKER_BASICAUTH_PASSWORD = "worker-pw"
INTEGRATION_WORKER_BASICAUTH_HASH = (
    "$2a$14$S2kt3Nx8OwKeINhWi7/Me.mIbgUzjomqkAG5baXNJfu7qCWXnnhJy"
)

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
    "backup",
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


def test_caddy_env_threads_basicauth_and_allow_cidrs(monkeypatch) -> None:
    """The expanded operator-facing Caddyfile (#19) reads three new
    environment variables that must be threaded through from the host
    `.env` into the caddy container:

      * BASICAUTH_USER / BASICAUTH_HASH — admin API credentials.
      * ADMIN_ALLOW_CIDRS — operator IP allow-list for the destructive
        commands endpoint.

    `docker compose config` ships these only if the compose file
    references them in caddy's `environment:` block. Without that
    wiring, `{$BASICAUTH_USER}` etc. expand to the empty string at
    Caddy load time and the basicauth directive silently accepts no
    one (or rejects everyone, depending on Caddy version).

    The test pre-sets the host env so the variables are non-empty in
    the rendered config — that's how the operator's filled-in `.env`
    will look in production."""
    # `docker compose config` escapes literal `$` in env values to `$$`
    # in its rendered output (so the rendered YAML is round-trip-safe
    # against compose's own interpolation). The container itself sees
    # the de-escaped value, but the test inspects the rendered YAML —
    # so use $-free sentinels that survive compose's normalisation
    # unchanged.
    monkeypatch.setenv("BASICAUTH_USER", "operator")
    monkeypatch.setenv("BASICAUTH_HASH", "BCRYPT-HASH-PLACEHOLDER")
    monkeypatch.setenv("ADMIN_ALLOW_CIDRS", "10.0.0.0/8")
    config = _compose_config()
    caddy = _service(config, "caddy")
    env = _service_env(caddy)

    assert env.get("BASICAUTH_USER") == "operator", (
        "caddy is not receiving BASICAUTH_USER from the host .env — "
        "the Caddyfile basicauth directive will load with an empty "
        "username (#19)"
    )
    assert env.get("BASICAUTH_HASH") == "BCRYPT-HASH-PLACEHOLDER", (
        "caddy is not receiving BASICAUTH_HASH from the host .env — "
        "the Caddyfile basicauth directive will load with an empty "
        "password hash (#19)"
    )
    assert env.get("ADMIN_ALLOW_CIDRS") == "10.0.0.0/8", (
        "caddy is not receiving ADMIN_ALLOW_CIDRS from the host .env "
        "— the commands-endpoint IP allow-list defaults wide-open (#19)"
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


def test_backup_service_is_declared() -> None:
    """Tracer for the backup story (#18): the production stack must
    declare a `backup` service. Everything else about it — image,
    volumes, env wiring — is asserted by the dedicated tests below.
    This one only catches a missing service stanza."""
    config = _compose_config()
    assert "backup" in config["services"], (
        "production stack is missing the `backup` service — postgres "
        "+ vmsingle volumes have no scheduled snapshot story (#18)"
    )


def test_backup_uses_ghcr_image_and_restart_policy() -> None:
    """The backup sidecar runs from our own GHCR image (built from
    packages/backup/Dockerfile) and restarts unless explicitly
    stopped — without `unless-stopped` a host reboot would leave the
    fleet running with no scheduled snapshots and no operator alert."""
    config = _compose_config()
    backup = _service(config, "backup")
    assert backup["image"].startswith("ghcr.io/axisaiblr/axis-backup"), (
        f"backup image must come from our GHCR namespace (consistent "
        f"with axis-control / axis-agent), got {backup['image']!r}"
    )
    assert backup.get("restart") == "unless-stopped", (
        "backup must restart=unless-stopped so a host reboot does not "
        "silently stop the scheduled snapshot loop"
    )


def test_backup_can_reach_postgres_and_read_vmsingle_volume() -> None:
    """The backup sidecar needs two inputs: an authenticated pg_dump
    connection to postgres, and read access to the vmsingle data
    directory so it can package the snapshot the VictoriaMetrics HTTP
    API drops there.

    `depends_on: postgres` ensures the database is running before the
    sidecar's first scheduled tick — pg_dump retries its own connect
    loop internally, so service_started is enough.

    `axis_vmsingle_data` must mount read-only — the backup process
    only reads snapshot dirs created by vmsingle; never writes."""
    config = _compose_config()
    backup = _service(config, "backup")

    deps = _depends_on(backup)
    assert "postgres" in deps, (
        "backup must depends_on postgres — pg_dump needs the database "
        "service running before the first scheduled tick"
    )

    vmsingle_mount = None
    for entry in backup.get("volumes", []):
        if isinstance(entry, dict) and entry.get("source") == "axis_vmsingle_data":
            vmsingle_mount = entry
            break
    assert vmsingle_mount is not None, (
        "backup must mount axis_vmsingle_data so it can read the "
        "vmsingle snapshot directory for tar/upload"
    )
    assert vmsingle_mount.get("read_only") is True, (
        "axis_vmsingle_data must be mounted read-only on the backup "
        "service — the backup process only reads vmsingle's snapshots, "
        f"never writes; got {vmsingle_mount!r}"
    )


def test_backup_has_writable_local_snapshot_volume() -> None:
    """The backup sidecar keeps a small rolling buffer of recent
    snapshots locally on a dedicated named volume. This recovers from
    the most common failure mode — a fat-fingered `docker compose
    down -v` on the management VPS — without waiting on a download
    from the offsite bucket.

    The volume must be declared at the top-level `volumes:` block (so
    it survives `docker compose down`) and mounted writable on backup."""
    config = _compose_config()
    backup = _service(config, "backup")

    local_mount = None
    for entry in backup.get("volumes", []):
        if isinstance(entry, dict) and entry.get("source") == "axis_backup_data":
            local_mount = entry
            break
    assert local_mount is not None, (
        "backup must mount axis_backup_data — without a local rolling "
        "buffer a `docker compose down -v` wipes every recent snapshot"
    )
    assert local_mount.get("read_only") is not True, (
        "axis_backup_data must be writable on backup — the sidecar "
        "writes its rolling snapshots here"
    )

    declared_volumes = config.get("volumes", {}) or {}
    assert "axis_backup_data" in declared_volumes, (
        "axis_backup_data is not declared in the top-level volumes: "
        "block; without it docker creates an anonymous volume on every "
        "`up` and the rolling buffer is lost on `down`"
    )


def _service_env(service: dict) -> dict[str, str]:
    """Normalise the service environment (dict or KEY=VAL list) to a
    plain dict. Mirrors the helper inline in the axis-control test."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return env


def test_backup_credentials_threaded_from_env_no_hardcoded() -> None:
    """S3 credentials for the offsite copy and postgres credentials for
    pg_dump both come from the host `.env`. None of these may be
    hard-coded into the image or the compose file:

      * S3 keys leaking into git would let a third party empty the
        backup bucket.
      * Postgres creds must reuse the same POSTGRES_* values the
        database itself uses — if they ever diverge, pg_dump silently
        starts failing while compose-up continues to look healthy."""
    config = _compose_config()
    backup = _service(config, "backup")
    env = _service_env(backup)

    # Offsite — Timeweb S3 endpoint + bucket + per-prefix scoping. All
    # mandatory; without an endpoint the aws-cli would default to AWS.
    assert env.get("AXIS_BACKUP_S3_ENDPOINT"), (
        "AXIS_BACKUP_S3_ENDPOINT must be set — without it the aws-cli "
        "defaults to AWS instead of the configured S3-compatible target"
    )
    assert env.get("AXIS_BACKUP_S3_BUCKET"), (
        "AXIS_BACKUP_S3_BUCKET must be set — there is no sensible default"
    )
    assert env.get("AXIS_BACKUP_S3_ACCESS_KEY_ID"), (
        "AXIS_BACKUP_S3_ACCESS_KEY_ID must be threaded from .env"
    )
    assert env.get("AXIS_BACKUP_S3_SECRET_ACCESS_KEY"), (
        "AXIS_BACKUP_S3_SECRET_ACCESS_KEY must be threaded from .env"
    )

    # Postgres creds must point at the same postgres service this
    # stack runs — drift between POSTGRES_USER on the db side and the
    # value the backup uses would silently break pg_dump.
    assert env.get("AXIS_BACKUP_POSTGRES_HOST") == "postgres", (
        "backup must connect to postgres by service DNS, got "
        f"{env.get('AXIS_BACKUP_POSTGRES_HOST')!r}"
    )
    assert env.get("AXIS_BACKUP_POSTGRES_USER"), (
        "backup needs POSTGRES_USER threaded — pg_dump connects with it"
    )
    assert env.get("AXIS_BACKUP_POSTGRES_PASSWORD"), (
        "backup needs POSTGRES_PASSWORD threaded — pg_dump authenticates "
        "with it"
    )
    assert env.get("AXIS_BACKUP_POSTGRES_DB"), (
        "backup needs POSTGRES_DB threaded — pg_dump targets it"
    )


def test_backup_schedule_and_retention_have_sane_defaults() -> None:
    """An operator who does not set AXIS_BACKUP_CRON or
    AXIS_BACKUP_LOCAL_RETENTION_DAYS in their .env should still get a
    working stack: daily snapshots at 02:00 UTC, 7 rolling local
    copies. These are the most asked-about config knobs but also the
    ones where a missing value would silently mean "no schedule" or
    "infinite local retention until the disk fills" — both are worse
    than a hard-coded default."""
    config = _compose_config()
    backup = _service(config, "backup")
    env = _service_env(backup)

    cron = env.get("AXIS_BACKUP_CRON")
    assert cron == "0 2 * * *", (
        "backup must default to a daily 02:00 UTC schedule when "
        f"AXIS_BACKUP_CRON is unset, got {cron!r}"
    )

    retention = env.get("AXIS_BACKUP_LOCAL_RETENTION_DAYS")
    assert retention == "7", (
        "backup must default to 7 local rolling snapshots when "
        f"AXIS_BACKUP_LOCAL_RETENTION_DAYS is unset, got {retention!r}"
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
    """The management plane is seven services. Any drift here is a
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


def _build_image_or_fail(dockerfile: Path, tag: str, name: str) -> str:
    """Shared helper used by the per-image session fixtures below."""
    if not _docker_daemon_available():
        pytest.skip("docker daemon not reachable")
    if not dockerfile.exists():
        pytest.fail(f"missing {dockerfile}")
    proc = subprocess.run(
        [
            "docker", "build",
            "-f", str(dockerfile),
            "-t", tag,
            str(REPO_ROOT),
        ],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"{name} image build failed:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return tag


@pytest.fixture(scope="session")
def axis_control_image() -> str:
    """Build the control-plane image locally and tag it as the value the
    production compose will look for when AXIS_CONTROL_IMAGE_TAG is set
    to `integration-test`. Session-scoped so the cost is paid once."""
    return _build_image_or_fail(
        CONTROL_DOCKERFILE, INTEGRATION_FULL_IMAGE, "axis-control"
    )


@pytest.fixture(scope="session")
def axis_backup_image() -> str:
    """Same idea for the backup sidecar (#18). The image is referenced
    by the production compose but not yet published to GHCR during a
    feature branch's CI run — build locally and tag it under the same
    `integration-test` rule the axis-control fixture uses."""
    return _build_image_or_fail(
        BACKUP_DOCKERFILE, INTEGRATION_BACKUP_IMAGE, "axis-backup"
    )


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
                "AXIS_CONTROL_REGISTRATION_TOKEN=integration-token",
                f"AXIS_CONTROL_IMAGE_TAG={INTEGRATION_IMAGE_TAG}",
                # `http://` scheme opts Caddy out of auto-HTTPS — there
                # is no ACME-issuable cert for `localhost` and we don't
                # want the redirect-to-https. Production VPS sets a real
                # domain with no scheme so Caddy negotiates Let's Encrypt
                # automatically.
                "ADMIN_DOMAIN=http://localhost",
                f"CADDY_HOST_PORT={caddy_host_port}",
                # Backup sidecar (#18). The schedule defaults to daily
                # 02:00 UTC and will never fire inside the test window,
                # but the entrypoint validates these env vars at
                # container start — they must be set (and non-empty)
                # or the container exits with code 1 and `--wait`
                # never reports the stack healthy. The endpoint is
                # deliberately unreachable to avoid surprise S3 calls.
                f"AXIS_BACKUP_IMAGE_TAG={INTEGRATION_IMAGE_TAG}",
                "AXIS_BACKUP_S3_ENDPOINT=http://nonexistent.invalid",
                "AXIS_BACKUP_S3_BUCKET=integration-bucket",
                "AXIS_BACKUP_S3_ACCESS_KEY_ID=integration-key",
                "AXIS_BACKUP_S3_SECRET_ACCESS_KEY=integration-secret",
                # Operator-facing Caddyfile (#19). The grafana site
                # address must NOT inherit ADMIN_DOMAIN's `http://`
                # prefix — caddy rejects `grafana.http://localhost`
                # as a site address. ADMIN_ALLOW_CIDRS defaults wide-
                # open at compose level; spell that out here for the
                # benefit of readers grepping for it.
                f"BASICAUTH_USER={INTEGRATION_BASICAUTH_USER}",
                # Compose interpolates `$VAR` references in `.env`
                # values. Bcrypt hashes embed segments like `$AOGr9G`
                # that compose otherwise eats — escape every `$` as
                # `$$` so the literal hash survives unchanged.
                f"BASICAUTH_HASH={INTEGRATION_BASICAUTH_HASH.replace('$', '$$')}",
                "GRAFANA_DOMAIN=http://grafana.localhost",
                "ADMIN_ALLOW_CIDRS=0.0.0.0/0",
                # Worker basicauth (#26). Same `$$`-doubling trick the
                # operator hash uses — bcrypt segments like `$AOGr9G`
                # would otherwise be eaten by compose's $-interpolation.
                # NATS_DOMAIN / VM_DOMAIN are split out from ADMIN_DOMAIN
                # for the same reason GRAFANA_DOMAIN is: ADMIN_DOMAIN
                # carries `http://` to opt caddy out of auto-HTTPS, so
                # `nats.${ADMIN_DOMAIN}` would render as the invalid site
                # address `nats.http://localhost`.
                f"WORKER_BASICAUTH_USER={INTEGRATION_WORKER_BASICAUTH_USER}",
                f"WORKER_BASICAUTH_HASH={INTEGRATION_WORKER_BASICAUTH_HASH.replace('$', '$$')}",
                "NATS_DOMAIN=http://nats.localhost",
                "VM_DOMAIN=http://vm.localhost",
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
      * sets pull_policy=never on axis-control and backup so docker
        compose uses the locally-built and locally-tagged images
        instead of trying to GET them from GHCR (the backup image is
        new on a feature branch and would not yet be published).
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
  backup:
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
    axis_backup_image: str,
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
    auth: tuple[str, str] | None = None,
) -> httpx.Response:
    """Poll `url` (with `Host: host_header`) until it returns 2xx or
    `timeout` elapses. Caddy's site blocks match on Host, so the test
    connects to 127.0.0.1 but advertises the configured admin domain
    — the same routing path real clients exercise.

    `auth` is forwarded to httpx as a `(user, password)` tuple for
    basicauth-protected endpoints; omit it for the public ones.

    The error surfaced on timeout names what actually went wrong
    (connection refused vs 5xx vs unmatched-vhost empty 200) so a
    failure is debuggable from the assertion message alone."""
    deadline = time.monotonic() + timeout
    last_err: str = "no attempt made"
    headers = {"Host": host_header}
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, headers=headers, timeout=3.0, auth=auth)
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
    axis_control_image: str, axis_backup_image: str, tmp_path: Path
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
                    headers={
                        "Host": host_header,
                        # Registration is gated by the bootstrap token
                        # (#8); the env file wires the same token into
                        # the control-plane container.
                        "Authorization": "Bearer integration-token",
                    },
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
        # `/api/instances/{id}` sits behind caddy basicauth (#19), so
        # the GET must present the operator credentials wired into the
        # integration env file.
        r = _retry_get(
            f"{base_url}/api/instances/{instance_id}",
            host_header=host_header,
            auth=(INTEGRATION_BASICAUTH_USER, INTEGRATION_BASICAUTH_PASSWORD),
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


@pytest.mark.production_compose_integration
def test_operator_facing_caddyfile_behaviors(integration_stack: _Stack) -> None:
    """End-to-end smoke for the operator-facing Caddyfile (#19):

      1. POST /api/instances is reachable with only a bearer token —
         agents must keep registering after caddy gains basicauth.
      2. GET /api/instances/{id} without basicauth returns 401 — the
         admin inventory is no longer reachable from the open internet.
      3. GET /api/instances/{id} with basicauth returns 200 — operators
         with the shared credential can still see the fleet.
      4. The grafana subdomain reverse-proxies to grafana — dashboards
         are reachable on the same VPS through caddy.

    Bundled into one stack lifetime to amortise the ~30 s startup. A
    failure in any step names the specific behaviour that broke."""
    base_url = f"http://127.0.0.1:{integration_stack.caddy_host_port}"
    admin_host = "localhost"
    grafana_host = "grafana.localhost"

    # (1) Register an instance — agent flow, no basicauth, only the
    # bootstrap bearer token (#8). Retry until the app is up.
    deadline = time.monotonic() + 90
    instance_id: str | None = None
    last_err = "no attempt"
    while time.monotonic() < deadline and instance_id is None:
        try:
            r = httpx.post(
                f"{base_url}/api/instances",
                json={
                    "project_name": "caddy-smoke",
                    "hostname": "caddy-smoke-1",
                },
                headers={
                    "Host": admin_host,
                    "Authorization": "Bearer integration-token",
                },
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
    assert instance_id, (
        f"agent registration through caddy never succeeded — POST "
        f"/api/instances should bypass basicauth (only bearer token "
        f"required). Last error: {last_err}"
    )

    # (2) Unauthenticated GET on the admin inventory must be blocked.
    r = httpx.get(
        f"{base_url}/api/instances/{instance_id}",
        headers={"Host": admin_host},
        timeout=5.0,
    )
    assert r.status_code == 401, (
        f"GET /api/instances/{{id}} without basicauth must 401 — caddy "
        f"basicauth is not gating the admin API as expected. Got "
        f"HTTP {r.status_code}: {r.text[:200]}"
    )

    # (3) Same GET with the operator credentials passes.
    r = httpx.get(
        f"{base_url}/api/instances/{instance_id}",
        headers={"Host": admin_host},
        auth=(INTEGRATION_BASICAUTH_USER, INTEGRATION_BASICAUTH_PASSWORD),
        timeout=5.0,
    )
    assert r.status_code == 200, (
        f"GET /api/instances/{{id}} with basicauth must 200 — the "
        f"operator's shared credential is not being accepted. Got "
        f"HTTP {r.status_code}: {r.text[:200]}"
    )
    assert r.json()["id"] == instance_id

    # (4) Grafana subdomain. Grafana's landing page is `GET /` which
    # redirects to /login (302) when unauthenticated; both responses
    # prove caddy is reverse-proxying to grafana:3000 successfully.
    r = httpx.get(
        f"{base_url}/",
        headers={"Host": grafana_host},
        follow_redirects=False,
        timeout=10.0,
    )
    assert r.status_code in (200, 301, 302), (
        f"grafana subdomain did not reverse-proxy to grafana:3000 — "
        f"got HTTP {r.status_code}: {r.text[:200]}"
    )


@pytest.mark.production_compose_integration
def test_worker_basicauth_gates_nats_ws_round_trip(
    integration_stack: _Stack,
) -> None:
    """Slice 4 (#26): a remote worker can publish→subscribe through the
    caddy-fronted NATS endpoint when it presents the shared worker
    basicauth credential. The internal nats broker is anonymous on the
    docker network; caddy is the only gate.

    This is the tracer-bullet for the whole NATS broker exposure
    feature: it forces every moving part into the test path —
    Caddyfile site-block for {$NATS_DOMAIN}, the WebSocket reverse-
    proxy, the basicauth directive, the new nats.conf with a websocket
    listener, the compose-wiring that mounts nats.conf, and the
    operator-side env vars. If this round-trip succeeds, the rest of
    the work is regression-defence.

    The test fixture connects to 127.0.0.1:<caddy_port> with
    Host: nats.localhost — same trick existing tests use to exercise
    caddy site routing without needing real DNS or TLS."""
    import asyncio

    import nats

    async def round_trip() -> str:
        # nats-py's WebSocket transport reads the Host header from the
        # URL; to make caddy route to the nats.localhost site block we
        # need the URL host to be `nats.localhost`. Build a connect URL
        # that points at 127.0.0.1 but force the Host through nats-py's
        # `tls_hostname` / server-side mapping is not available — use
        # an explicit servers= list where the host is nats.localhost
        # and the port is the kernel-assigned caddy port. `nats.localhost`
        # resolves to 127.0.0.1 on every OS we deploy on.
        servers = [
            f"ws://{INTEGRATION_WORKER_BASICAUTH_USER}:"
            f"{INTEGRATION_WORKER_BASICAUTH_PASSWORD}"
            f"@nats.localhost:{integration_stack.caddy_host_port}"
        ]
        nc = await nats.connect(servers=servers, connect_timeout=10)
        try:
            sub = await nc.subscribe("test.tracer")
            await nc.flush(timeout=5)
            await nc.publish("test.tracer", b"hello-worker")
            msg = await asyncio.wait_for(sub.next_msg(timeout=5), timeout=10)
            return msg.data.decode()
        finally:
            await nc.drain()

    received = asyncio.run(round_trip())
    assert received == "hello-worker", (
        f"WSS round-trip through caddy → nats failed at the assertion "
        f"layer. Connection opened but payload was {received!r}."
    )


@pytest.mark.production_compose_integration
def test_worker_basicauth_missing_returns_401_on_nats_ws(
    integration_stack: _Stack,
) -> None:
    """Slice 5 (#26): negative-path defence for the NATS WSS gateway.
    Without basicauth Caddy must 401 before the WebSocket upgrade gets
    anywhere near the broker. Plain HTTP GET on the upgrade endpoint
    is the cheapest reliable probe — Caddy basicauth runs before the
    upgrade matcher, so a missing Authorization header returns 401
    regardless of whether the client meant to speak WebSocket. If this
    test goes red while slice 4 still passes, somebody removed the
    `basicauth` directive from the {$NATS_DOMAIN} site-block."""
    url = f"http://127.0.0.1:{integration_stack.caddy_host_port}/"
    r = httpx.get(url, headers={"Host": "nats.localhost"}, timeout=5.0)
    assert r.status_code == 401, (
        f"NATS WSS endpoint without basicauth must 401 — caddy is "
        f"letting traffic through to the broker. Got HTTP "
        f"{r.status_code}: {r.text[:200]}"
    )


@pytest.mark.production_compose_integration
def test_worker_basicauth_wrong_returns_401_on_nats_ws(
    integration_stack: _Stack,
) -> None:
    """Slice 6 (#26): basicauth actually validates the credential —
    presenting a syntactically-valid but wrong user/password pair must
    still 401, not pass through because "an Authorization header is
    present". Defends against future Caddyfile edits that accidentally
    weaken the directive (e.g. flipping the order so the matcher
    short-circuits the check)."""
    url = f"http://127.0.0.1:{integration_stack.caddy_host_port}/"
    r = httpx.get(
        url,
        headers={"Host": "nats.localhost"},
        auth=("not-the-worker", "not-the-password"),
        timeout=5.0,
    )
    assert r.status_code == 401, (
        f"NATS WSS endpoint with wrong basicauth must 401 — caddy is "
        f"accepting credentials it should reject. Got HTTP "
        f"{r.status_code}: {r.text[:200]}"
    )

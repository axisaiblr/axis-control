# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Authentication between agent, control plane, and NATS (#8). Two
  layers of identity, both opaque random tokens:
  * **Bootstrap registration token** — shared secret configured on the
    control plane as `AXIS_CONTROL_REGISTRATION_TOKEN`; agents present
    it as `Authorization: Bearer <token>` on `POST /api/instances`.
    Without it the endpoint refuses every request (production-safe
    default; the value mirrors to each worker as
    `AXIS_AGENT_REGISTRATION_TOKEN`).
  * **Per-instance agent token** — minted at registration, returned to
    the agent exactly once in the 201 response, persisted on the
    instance row plus in the agent's identity store. Every
    `status.<id>`, `heartbeat.<id>`, and `commands.<id>` message
    carries this token in its envelope; the control plane verifies
    inbound messages and the agent verifies inbound commands in
    constant time. Mismatching messages are dropped with a log warning
    — late or spoofed publishes can no longer finalise a command,
    flip reachability, or impersonate the control plane on an open
    broker.
- New control-plane config `AXIS_CONTROL_REGISTRATION_TOKEN` and agent
  config `AXIS_AGENT_REGISTRATION_TOKEN`; both compose templates
  (production + worker) thread the value through. `.env.example`
  documents both with a `secrets.token_urlsafe(32)` recipe.
- New repo helper `axis_control.domain.auth.mint_agent_token` /
  `verify_agent_token` (constant-time compare). Dispatching a command
  to an instance with no persisted token returns HTTP 404 instead of
  publishing an unstampable message.
- Worker `docker-compose.worker.yml` template that ships the `axis-agent`
  sidecar as a drop-in next to a project's own compose file on a worker
  VPS. Run both compose files together under one project name; the
  template parameterises the agent over
  `AXIS_AGENT_{PROJECT_NAME,CONTROL_PLANE_URL,NATS_URL,COMPOSE_FILE}`
  (+ optional tag / hostname / heartbeat overrides), bind-mounts
  `/var/run/docker.sock` and the project compose at the same host
  path inside the container (so `docker compose -f` resolves
  identically on both sides), defaults `AXIS_AGENT_COMPOSE_MODE` to
  `docker` (the dev `logging` default would silently no-op every
  command on a worker), and pins the agent's identity cache to a named
  volume `axis_agent_state` so a restart does not re-register the
  worker as a fresh instance.
- New pytest marker `worker_compose` and static checks under
  `tests/test_worker_compose.py` (parsed via `docker compose config`).
- `.env.example` extended with a new worker-stack section documenting
  the required and optional `AXIS_AGENT_*` env vars for the worker
  template.
- Production `docker-compose.yml` for the management VPS, plus a minimal
  `caddy/Caddyfile`. One file, one host, one command:
  `cp .env.example .env && docker compose up -d` brings up caddy (TLS
  via Let's Encrypt for `${ADMIN_DOMAIN}`), postgres (named volume),
  nats (internal-only, no host port until auth lands in #8),
  axis-control (image from GHCR, wired by service-DNS to postgres + nats,
  fronted by caddy), vmsingle (VictoriaMetrics, named volume,
  configurable retention), and grafana (admin pw from env, named volume).
  Caddy reverse-proxies the admin domain to axis-control:8000; grafana
  routing and Let's-Encrypt-staging defaults are follow-ups.
- `.env.example` extended with a production-stack section
  (`POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `AXIS_CONTROL_IMAGE_TAG`,
  `ADMIN_DOMAIN`) alongside the existing local-dev section. Documents the
  `http://`-scheme opt-out for non-ACME-issuable hostnames.
- Two new pytest markers for the production compose:
  - `production_compose` — static checks via `docker compose config`,
    fast, runs on every test invocation.
  - `production_compose_integration` — brings the full stack up, exercises
    caddy → axis-control `/healthz`, and verifies postgres data survives a
    `docker compose down && up`. Slow; deselect with
    `-m 'not production_compose_integration'`.
  Tests live at `tests/test_production_compose.py` (new top-level
  `tests/` directory, added to `pytest.testpaths`).
- `pyyaml` as a dev dependency (compose-config parsing in the new tests).
- Dockerfiles for `axis-control` and `axis-agent`, published to GHCR
  as `ghcr.io/axisaiblr/axis-control` and `ghcr.io/axisaiblr/axis-agent`.
  Multi-stage build: `ghcr.io/astral-sh/uv` builder syncs the workspace
  into a self-contained venv, copied into a `python:3.12-slim-bookworm`
  runtime. Console scripts (`axis-control`, `axis-agent`) are the
  image ENTRYPOINTs. The agent image additionally carries
  `docker-ce-cli` + `docker-compose-plugin` so it can shell out to
  `docker compose` against the worker host's mounted
  `/var/run/docker.sock`.
- `.github/workflows/docker-publish.yml`: builds + pushes both images.
  On push to `main` images are tagged `:edge`. On a `v*.*.*` tag they
  are tagged `:MAJOR.MINOR.PATCH`, `:MAJOR.MINOR`, and `:latest`. Uses
  `docker/build-push-action` + GHA build cache scoped per image.
  linux/amd64 only for v1.
- New pytest marker `docker_image` for the slow build-and-run tests
  under `packages/{control,agent}/tests/test_docker_image.py`. Skip
  during fast loops with `-m 'not docker_image'`.
- Instance reachability driven by agent heartbeats. The agent now
  publishes a `HeartbeatMessage` on `heartbeat.<instance_id>` once
  immediately after starting its command subscription and then every
  `AXIS_AGENT_HEARTBEAT_INTERVAL_SECONDS` (default 10 s). The control
  plane subscribes to `heartbeat.>`, bumps `instances.last_heartbeat_at`,
  and derives `reachability` (`unknown` / `online` / `offline`) from a
  configurable freshness window
  (`AXIS_CONTROL_HEARTBEAT_STALE_SECONDS`, default 30 s).
- New `HeartbeatPublisher` deep module in `axis_agent` and
  `HeartbeatSubscriber` adapter in `axis_control`.
- Command timeout sweeper: pending commands now reach a terminal `failed`
  state within a bounded time even when no agent ever consumes the NATS
  message. New `commands.failure_reason` column carries a stable token
  (`no_acknowledgement_within_timeout`) that UI and tooling can match on.
  Workload state is *not* inferred from a timeout — only the command
  row is finalised. Configured via
  `AXIS_CONTROL_COMMAND_TIMEOUT_SECONDS` (default 60) and
  `AXIS_CONTROL_COMMAND_SWEEP_INTERVAL_SECONDS` (default 5).
- Delivery hint on command dispatch: `POST /api/instances/{id}/commands`
  now returns a `delivery` field — `delivered_now` when a subscriber was
  reachable at publish time, `no_listeners` when the NATS broker reported
  no responders, `unknown` on transient publish errors. Implemented via a
  short-lived NATS request probe configured by
  `AXIS_CONTROL_NATS_PUBLISH_PROBE_TIMEOUT` (default 0.1 s).
- `axis-agent` self-registration: on first start, the agent calls
  `POST /api/instances` itself using `AXIS_AGENT_PROJECT_NAME` and
  `AXIS_AGENT_HOSTNAME` (defaults to OS hostname), persists the
  assigned UUID under `AXIS_AGENT_STATE_DIR/instance.json`, and reuses
  it across restarts. First-time registration retries with bounded
  exponential backoff before exiting non-zero.
- `axis-agent --reset-identity` CLI flag: deletes the cached
  `instance.json` so the next start re-registers cleanly.
- Two new deep modules in `axis_agent`:
  - `identity.AgentIdentityStore` — JSON-on-disk persistence of the
    assigned instance UUID.
  - `control_plane.ControlPlaneClient` — thin async httpx wrapper for
    the control-plane API (`register(project_name, hostname) -> UUID`).
  - `registration.ensure_identity` orchestrates override → persisted
    state → self-register-with-backoff.
- Monorepo scaffold (uv workspace) with `axis-shared`, `axis-control`, `axis-agent`.
- `POST /api/instances/{id}/commands` — issue disable/enable, persists as
  pending and publishes `CommandMessage` to NATS `commands.<instance_id>`.
- `GET /api/commands/{id}` — query command status.
- Inbound status subscriber on `status.>` — completes the matching command
  and flips the instance's workload state (disable → disabled,
  enable → enabled).
- `POST /api/instances` — register a worker; project is created on first use.
- `GET /api/instances/{id}` — read current instance state.
- Hexagonal layering: `domain/` (pure), `adapters/` (postgres, nats),
  `services/` (orchestration), `api/` (FastAPI routes).
- `axis-agent` package: worker-side sidecar that subscribes to NATS
  `commands.<instance_id>`, drives a `ComposeRunner` port
  (disable→stop, enable→start), and publishes a `StatusMessage` with
  completion or failure on `status.<instance_id>`.
- Integration test harness with testcontainers (real Postgres + NATS).
- Production entrypoints: `axis-control` and `axis-agent` console scripts,
  pydantic-settings-driven config (env / .env), idempotent schema apply on
  startup, graceful SIGINT/SIGTERM shutdown.
- `docker-compose.dev.yml` for local Postgres + NATS, `.env.example` and a
  README quickstart that walks through the full disable round-trip.
- `LoggingComposeRunner` (default dry-run, safe on dev machines) and
  `DockerComposeRunner` (real `docker compose stop/start` via subprocess)
  selected by `AXIS_AGENT_COMPOSE_MODE`.

### Changed
- Instance status is split into two orthogonal API fields:
  - `workload_state` (`unknown` / `enabled` / `disabled`) — the
    operator's last expressed intent, only flipped by a successful
    enable/disable command. Replaces the old `status` field; the legacy
    `running` value is now `enabled`.
  - `reachability` (`unknown` / `online` / `offline`) — derived from
    `last_heartbeat_at`, not stored.
  Both fields plus `last_heartbeat_at` are returned by `GET` /
  `POST /api/instances*`. The legacy `status` field is removed
  (no backwards-compat shim — there are no clients yet).
- Database schema: `instances.status` column replaced with
  `instances.workload_state`, plus a new `instances.last_heartbeat_at`
  column. Dev DBs upgrade in place via the idempotent DDL block;
  existing `running` rows are migrated to `enabled`.
- Inbound status reports are now ignored when the targeted command is
  already in a terminal state. Terminal means terminal: a late
  `completed` from an agent that came online after the timeout fired
  does not resurrect a `failed` row or flip the workload state. The
  late report is logged at WARNING level as an anomaly.
- `axis-agent` required env: `AXIS_AGENT_INSTANCE_ID` is no longer
  mandatory. The agent now requires `AXIS_AGENT_PROJECT_NAME` and
  `AXIS_AGENT_CONTROL_PLANE_URL`. `AXIS_AGENT_INSTANCE_ID` is still
  accepted as an override (bypasses both the persisted state and the
  self-registration step).
- README quickstart no longer asks the operator to copy a UUID by hand
  from `curl POST /api/instances` into `.env`. `uv run axis-agent`
  works directly after editing `project_name` + control plane URL.

### Fixed
- NATS subscribe-then-publish race: `Agent.start()`,
  `StatusSubscriber.start()`, and `HeartbeatSubscriber.start()` now
  `flush()` after `subscribe()`, so they block until the broker has
  acknowledged the SUB. Without this, a message published the instant
  `start()` returned could reach the broker before the subscription
  was registered and be dropped to no-listeners. Surfaced as
  intermittent failures of
  `test_agent_executes_disable_and_reports_completed_status` on
  Windows (NATS-on-Docker-Desktop latency widened the race window),
  but the bug existed in production too — most relevant for the agent,
  since an operator could issue a command immediately after a fresh
  agent registers.
- `agent_nc` test fixture now closes with `nc.close()` instead of
  `nc.drain()`. `drain()` did UNSUB-then-close while the read loop was
  still running, which races nats-py's PONG handler and surfaced as
  `asyncio.InvalidStateError: invalid state` at teardown.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

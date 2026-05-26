# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
  and flips the instance's state (disable → disabled, enable → running).
- `POST /api/instances` — register a worker; project is created on first use.
- `GET /api/instances/{id}` — read current instance state.
- Hexagonal layering: `domain/` (pure), `adapters/` (postgres, nats),
  `services/` (orchestration), `api/` (FastAPI routes).
- `axis-agent` package: worker-side sidecar that subscribes to NATS
  `commands.<instance_id>`, drives a `ComposeRunner` port
  (disable→stop, enable→start), and publishes a `StatusMessage` with
  completion or failure on `status.<instance_id>`.
- Integration test harness with testcontainers (real Postgres + NATS),
  ~8 seconds for the full 4-test suite on Windows + Docker Desktop.
- Production entrypoints: `axis-control` and `axis-agent` console scripts,
  pydantic-settings-driven config (env / .env), idempotent schema apply on
  startup, graceful SIGINT/SIGTERM shutdown.
- `docker-compose.dev.yml` for local Postgres + NATS, `.env.example` and a
  README quickstart that walks through the full disable round-trip
  (register → issue disable → agent dry-run → status flips to disabled).
- `LoggingComposeRunner` (default dry-run, safe on dev machines) and
  `DockerComposeRunner` (real `docker compose stop/start` via subprocess)
  selected by `AXIS_AGENT_COMPOSE_MODE`.

### Changed
- `axis-agent` required env: `AXIS_AGENT_INSTANCE_ID` is no longer
  mandatory. The agent now requires `AXIS_AGENT_PROJECT_NAME` and
  `AXIS_AGENT_CONTROL_PLANE_URL`. `AXIS_AGENT_INSTANCE_ID` is still
  accepted as an override (bypasses both the persisted state and the
  self-registration step).
- README quickstart no longer asks the operator to copy a UUID by hand
  from `curl POST /api/instances` into `.env`. `uv run axis-agent`
  works directly after editing `project_name` + control plane URL.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

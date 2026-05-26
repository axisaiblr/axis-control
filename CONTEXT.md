# axis-control — project context

This file is the canonical handoff document for the project. It is meant
to be read by anyone (human or AI agent) arriving at the repo for the
first time. Keep it short, accurate, and current — when you change the
shape of the system, update the relevant section here in the same commit.

## What this is

`axis-control` is the control plane for AXIS AI's worker fleet. It runs
on one management VPS and lets an operator inventory, observe, and
remotely disable / enable per-project workloads (text-assistant,
voice-assistant, …) running on separate worker VPSes.

The architecture is intentionally minimal: pull the prebuilt container
images, fill an `.env`, `docker compose up`. No Ansible, no Semaphore,
no k8s.

## Architecture in one picture

```
                  +----------------------------------------+
                  |   Management VPS (single host)         |
                  |                                        |
operator <-HTTP-->|   axis-control (FastAPI)               |
                  |     |             ^                    |
                  |     v             |                    |
                  |   Postgres     NATS broker             |
                  |                   ^   ^                |
                  +-------------------+---+----------------+
                                      |   |
                          commands.<id>   status.<id> / heartbeat.<id>
                                      |   |
                  +-------------------+---+----------------+
                  |   Worker VPS (one per deploy)          |
                  |                                        |
                  |   axis-agent sidecar  ----+            |
                  |                           |            |
                  |   docker compose  <-------+            |
                  |     (text-assistant or voice-assistant)|
                  +----------------------------------------+
```

The **agent is the only thing on a worker VPS that talks to the
management VPS**, and it does so over a single outbound NATS connection.
No inbound port needs to be open on the worker.

## Domain glossary

These terms have one meaning across the codebase. Don't introduce
synonyms; if a new concept appears, add it here.

- **Control plane** — the management-VPS process. Synonyms in code:
  `axis-control`, the FastAPI app, the admin API.
- **Worker** — a VPS that runs one project's docker compose stack plus
  the `axis-agent` sidecar.
- **Agent** — the sidecar process on a worker. One agent = one
  `instance_id`. Talks to the control plane only over NATS (and HTTP at
  registration time).
- **Project** — a named workload type (`text-assistant`,
  `voice-assistant`). A row in `projects`.
- **Instance** — one running deployment of a project on one worker.
  A row in `instances`. Identified by a UUID.
- **Command** — an operator-initiated action targeted at one instance
  (`disable`, `enable`). A row in `commands`. Has a status.
- **Status report** — message published by an agent on `status.<id>`
  to announce the outcome of a command.
- **Heartbeat** *(planned, #3)* — periodic liveness signal published by
  the agent on `heartbeat.<id>`. Drives the reachability axis of the
  instance state.
- **Workload state** *(planned, #3)* — the operator's last-expressed
  intent for an instance (`enabled` / `disabled`). Orthogonal to
  reachability.
- **Reachability** *(planned, #3)* — derived from `last_heartbeat_at`
  (`online` / `offline` / `unknown`). Not stored; computed on read.

## NATS subject taxonomy

- `commands.<instance_id>` — control plane → agent. Targeted by id.
  Agent subscribes after startup.
- `status.<instance_id>` — agent → control plane. Outcome of a command.
  Control plane subscribes with wildcard `status.>`.
- `heartbeat.<instance_id>` *(planned, #3)* — agent → control plane.
  Periodic, every ~10 s. Wildcard `heartbeat.>`.

## Repository layout

```
packages/
├── shared/         # NATS message schemas; protocol enums; subject helpers
├── control/        # FastAPI app + asyncpg + nats; domain/adapters/services/api
└── agent/          # worker-side sidecar + compose runners
docker-compose.dev.yml  # local Postgres + NATS for dev
```

Internal layering of `control` is hexagonal: `domain` (pure), `adapters`
(postgres, nats), `services` (orchestration that composes ports), `api`
(FastAPI routes wired through DI). Hold this line; do not let
adapters/services leak FastAPI types or asyncpg types into `domain`.

## What's built (v0.1)

End-to-end disable / enable loop, covered by 4 integration tests (~8 s
on Windows + Docker Desktop):

- `POST /api/instances` — register a worker (auto-creates project).
- `POST /api/instances/{id}/commands` — issue `disable` / `enable`,
  persists pending row, publishes `CommandMessage` to NATS.
- Agent subscribes to `commands.<its-id>`, runs its `ComposeRunner`
  (`LoggingComposeRunner` dry-run by default, `DockerComposeRunner` for
  real), publishes `StatusMessage` on `status.<id>`.
- Control plane's `StatusSubscriber` finalises the command row and
  flips instance `status`.

Console scripts: `axis-control`, `axis-agent`. Both load config from
env (`AXIS_CONTROL_*`, `AXIS_AGENT_*`) and a `.env` file. Idempotent
schema is applied on startup. Graceful SIGINT/SIGTERM shutdown.

## What's in motion (open issues)

Live on GitHub at <https://github.com/axisaiblr/axis-control/issues>.

**PRD #4** — bundles the three bug-class issues below into one
narrative. The actual work units are #1, #2, #3.

- **#1** *(enhancement)* — agent self-registers on startup; removes
  the manual UUID-copy step. Highest-leverage UX fix.
- **#2** *(bug)* — commands published with no subscriber stay pending
  forever; needs timeout + delivery hint.
- **#3** *(enhancement)* — split instance status into reachability
  (heartbeat-driven) and workload state (operator intent).

Recommended order: #1 → #3 → #2.

## What's not yet planned in detail (roadmap)

Filed as `needs-triage` issues so they don't get forgotten, but each
needs its own design conversation before becoming actionable:

- **Dockerfile for control + agent → GHCR pipeline.**
- **Production `docker-compose.yml` for the management VPS** (caddy,
  postgres, nats, axis-control, grafana, vmsingle).
- **`docker-compose.worker.yml` template** (worker app + axis-agent
  sidecar in one file, env-driven).
- **Authentication** between agent and control plane (NATS user JWTs?
  instance tokens? mTLS?). Currently the broker is open.
- **Admin UI** — HTMX pages over the existing API.
- **Custom per-project metrics** — vmagent on each worker scrapes
  project metrics, ships to vmsingle on the management VPS; Grafana
  dashboards.

## Conventions

- **TDD** — every behaviour change starts with a failing test. Vertical
  slice (one test = one user-visible behaviour). RED → GREEN → REFACTOR.
  Do not refactor while red.
- **Real infrastructure in tests** — Postgres and NATS via
  testcontainers, no mocking of adapters. Pure logic in `domain` and
  small `services` stays plain-Python testable.
- **Deep modules** — each new concept gets a small interface and is
  testable in isolation. Avoid "helpers" / "utils" modules; everything
  has a real home in `domain` / `adapters` / `services` / `api`.
- **Branching** — `main` is always green and deployable in principle.
  Work happens on `dev`. Feature branches optional for parallel work.
- **Commits** — Conventional Commits (`feat:`, `fix:`, `docs:`,
  `chore:`, `refactor:`, `test:`, `ci:`). Manifest + lockfile in the
  same commit.

## Local quickstart

See `README.md`. Tl;dr: `docker compose -f docker-compose.dev.yml up -d`,
`uv sync --all-packages`, `cp .env.example .env`, `uv run axis-control`
in one terminal, `uv run axis-agent` in another.

## Things known to bite

- **Windows + Docker Desktop:** always use `127.0.0.1`, never
  `localhost`, in Python async clients. `localhost` resolves IPv6 first
  and adds a ~5 s stall to TCP connect. Encoded in `packages/conftest.py`
  and in the runtime defaults; remember it when adding new fixtures or
  config.
- **`asyncpg` schema-name escaping:** when adding new SQL, parameterise
  values, never identifiers. Stick to the `$1, $2, …` placeholder style
  used throughout the existing repository.
- **pytest conftest collision** — the workspace runs in
  `--import-mode=importlib` mode and each tests directory deliberately
  has no `__init__.py`. Don't add one; conftests will collide on the
  same dotted name.

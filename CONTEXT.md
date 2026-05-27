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
  to announce the outcome of a command. Only applied if the command is
  still pending; late reports are logged and ignored.
- **Delivery hint** — best-effort indicator returned on
  `POST .../commands` (`delivered_now` / `no_listeners` / `unknown`).
  Captured via a short NATS request probe at publish time. Informational
  only; the persisted row is the source of truth.
- **Timeout sweeper** — background task that fails pending commands
  older than `command_timeout_seconds` with the stable reason
  `no_acknowledgement_within_timeout`. Does not change instance state.
- **Heartbeat** — periodic liveness signal published by the agent on
  `heartbeat.<id>`, every `AXIS_AGENT_HEARTBEAT_INTERVAL_SECONDS`
  (default 10 s) plus one immediate beat at startup. Drives the
  reachability axis of the instance state.
- **Workload state** — the operator's last-expressed intent for an
  instance (`unknown` / `enabled` / `disabled`). Stored on the instance
  row. Only flipped by a successful enable/disable command — survives
  the agent going offline.
- **Reachability** — derived from `last_heartbeat_at`
  (`unknown` / `online` / `offline`); offline once the heartbeat is
  older than `AXIS_CONTROL_HEARTBEAT_STALE_SECONDS` (default 30 s).
  Not stored; computed on read.
- **Bootstrap registration token** — shared secret configured on the
  control plane as `AXIS_CONTROL_REGISTRATION_TOKEN`. Agents present
  it as `Authorization: Bearer …` on `POST /api/instances`. No token
  configured → every request 401s. Same value mirrored to each worker
  as `AXIS_AGENT_REGISTRATION_TOKEN`.
- **Agent token** — opaque per-instance secret minted by the control
  plane at registration, persisted on the instance row and in the
  agent's identity store. Stamped into every NATS message
  (`status.<id>`, `heartbeat.<id>`, `commands.<id>`); the control
  plane and agent both verify it in constant time and silently drop
  mismatches.
- **Broker URL** — the externally-reachable NATS endpoint workers
  connect to. Production value is `wss://nats.${ADMIN_DOMAIN}`,
  TLS-terminated and basicauth-gated by Caddy, which reverse-proxies
  the WebSocket upgrade onto the internal `nats://nats:4222`. Inside
  the management-VPS docker network services keep using the internal
  URL; only agents on remote workers go through the broker URL.
- **Caddy extras volume** — shared docker volume `axis_caddy_extras`,
  declared `external: true` in `docker-compose.yml` and mounted on the
  `caddy` service at `/etc/caddy/extras`. Sibling compose stacks on
  the same VPS (`axis-infisical` is the first consumer; future admin
  UIs are next) drop a `*.caddy` site-block fragment into this volume
  and axis-control's Caddyfile picks it up via the top-level
  `import /etc/caddy/extras/*.caddy`. Empty directory is fine —
  `import` no-ops when the glob matches nothing. Conflict detection
  (two consumers claiming the same site address) is Caddy's job at
  parse time: startup error, not silent overwrite. Operator bootstrap:
  `docker volume create axis_caddy_extras` once on the VPS; manual
  reload after a fragment changes:
  `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.
  Rationale and rejected alternatives in
  [axis-infisical ADR-0002](https://github.com/axisaiblr/axis-infisical/blob/main/docs/adr/0002-caddy-extras-import-pattern.md).
- **Worker basicauth** — shared HTTP basicauth pair
  (`WORKER_BASICAUTH_USER` / `WORKER_BASICAUTH_HASH` on the management
  VPS, the matching plaintext password on each worker) gating the
  broker URL and the vmsingle remote-write endpoint
  (`vm.${ADMIN_DOMAIN}/api/v1/write`). One pair across the whole
  fleet; deliberately distinct from the admin-API basicauth
  (`BASICAUTH_*`) introduced for operators in #19. Threat model:
  rotation is fleet-wide. Impersonation is still blocked by the
  per-instance [[agent token]]; the broker is anonymous on the
  internal network.

## NATS subject taxonomy

- `commands.<instance_id>` — control plane → agent. Targeted by id.
  Agent subscribes after startup.
- `status.<instance_id>` — agent → control plane. Outcome of a command.
  Control plane subscribes with wildcard `status.>`.
- `heartbeat.<instance_id>` — agent → control plane. Periodic, every
  `AXIS_AGENT_HEARTBEAT_INTERVAL_SECONDS` (default 10 s). Control plane
  subscribes with wildcard `heartbeat.>`.

## Repository layout

```
packages/
├── shared/         # NATS message schemas; protocol enums; subject helpers
├── control/        # FastAPI app + asyncpg + nats; domain/adapters/services/api
└── agent/          # worker-side sidecar + compose runners
tests/                  # workspace-level tests (production compose)
docker-compose.yml      # production management-plane stack
docker-compose.dev.yml  # local Postgres + NATS for dev
caddy/Caddyfile         # reverse-proxy config used by docker-compose.yml
```

Internal layering of `control` is hexagonal: `domain` (pure), `adapters`
(postgres, nats), `services` (orchestration that composes ports), `api`
(FastAPI routes wired through DI). Hold this line; do not let
adapters/services leak FastAPI types or asyncpg types into `domain`.

## What's built (v0.1)

End-to-end disable / enable loop, agent self-registration, and the
two-axis reachability + workload-state model. Covered by integration
tests against real Postgres + NATS via testcontainers:

- `POST /api/instances` — register a worker (auto-creates project).
- `POST /api/instances/{id}/commands` — issue `disable` / `enable`,
  persists pending row, publishes `CommandMessage` to NATS.
- Agent subscribes to `commands.<its-id>`, runs its `ComposeRunner`
  (`LoggingComposeRunner` dry-run by default, `DockerComposeRunner` for
  real), publishes `StatusMessage` on `status.<id>`.
- Control plane's `StatusSubscriber` finalises the command row and
  flips instance `workload_state` (`enable → enabled`,
  `disable → disabled`).
- Agent **self-registers** on first start: reads `project_name` +
  `hostname` + `control_plane_url`, calls the registration endpoint,
  persists the assigned UUID to `state_dir/instance.json`, reuses it
  across restarts. `AXIS_AGENT_INSTANCE_ID` remains as an override.
- Agent publishes a `HeartbeatMessage` on `heartbeat.<id>` immediately
  after subscribing to commands, then every
  `AXIS_AGENT_HEARTBEAT_INTERVAL_SECONDS`. Control plane's
  `HeartbeatSubscriber` updates `instances.last_heartbeat_at`, and the
  API derives `reachability` from it. Workload state and reachability
  move on independent axes — a disabled instance whose agent dies
  keeps `workload_state: disabled` and flips to `reachability: offline`.

Console scripts: `axis-control`, `axis-agent`. Both load config from
env (`AXIS_CONTROL_*`, `AXIS_AGENT_*`) and a `.env` file. Idempotent
schema is applied on startup. Graceful SIGINT/SIGTERM shutdown.

Agent deep modules: `identity.AgentIdentityStore` (file-backed JSON),
`control_plane.ControlPlaneClient` (thin httpx wrapper),
`registration.ensure_identity` (override → load → register-with-backoff),
`heartbeat.HeartbeatPublisher` (immediate beat + interval loop).

Control deep modules added for the reachability split:
`adapters.nats_heartbeat.HeartbeatSubscriber` (wildcard `heartbeat.>`,
bumps `last_heartbeat_at` on receipt), `domain.models.reachability_of`
(pure function over `last_heartbeat_at + now + stale_after`).

**Distribution.** Both packages ship as OCI images on GHCR
(`ghcr.io/axisaiblr/axis-control`, `ghcr.io/axisaiblr/axis-agent`)
built from `packages/{control,agent}/Dockerfile`. Multi-stage: `uv`
builder produces a self-contained venv that's copied into a
`python:3.12-slim-bookworm` runtime; the console scripts are the
ENTRYPOINTs. The agent image additionally carries `docker-ce-cli` +
`docker-compose-plugin` so it can shell out to `docker compose`
against the worker host's bind-mounted `/var/run/docker.sock`.
`.github/workflows/docker-publish.yml` builds + pushes on push to
`main` (tagged `:edge`) and on `v*.*.*` tags (`:MAJOR.MINOR.PATCH`,
`:MAJOR.MINOR`, `:latest`). linux/amd64 only for v1; tests in
`packages/{control,agent}/tests/test_docker_image.py` (marker:
`docker_image`) verify the entrypoint, ENTRYPOINT directive, and —
for the agent — the docker CLI + compose plugin against a real
build.

**Worker-plane deployment.** `docker-compose.worker.yml` at the repo
root ships the `axis-agent` sidecar as a drop-in next to the project's
own compose file on each worker VPS. The operator runs both compose
files together under one project name. The template bind-mounts
`/var/run/docker.sock` and the host path the operator sets in
`AXIS_AGENT_COMPOSE_FILE` (at the same path inside the container, so
`docker compose -f <path>` resolves identically on both sides), pins
the agent's identity cache to a named volume `axis_agent_state` so a
restart does not re-register the worker, and defaults
`AXIS_AGENT_COMPOSE_MODE` to `docker` (the dev `logging` default would
silently drop every command on a production worker). Required env:
`AXIS_AGENT_{PROJECT_NAME,CONTROL_PLANE_URL,NATS_URL,REGISTRATION_TOKEN,COMPOSE_FILE}`.
Verified by `tests/test_worker_compose.py` (marker: `worker_compose`)
which parses the rendered config and asserts the agent's image,
restart policy, env wiring, both bind mounts, and the named state
volume.

**Authentication.** Every cross-host call is authenticated end-to-end
via two opaque tokens (#8):

- HTTP registration is gated by `AXIS_CONTROL_REGISTRATION_TOKEN` (a
  shared bootstrap secret). Without it `POST /api/instances` refuses
  every request — production-safe default; dev/test wiring sets the
  value explicitly.
- At registration the control plane mints a per-instance `agent_token`,
  returns it in the 201 response, and persists it on the instance row.
  The agent persists the same plaintext in its identity store
  (`instance.json`).
- Every NATS message stamps that token in its envelope. Status and
  heartbeat subscribers compare it to the stored copy and silently
  drop mismatches — late or spoofed reports cannot finalise a command
  or flip reachability. The control plane's command publisher stamps
  the same token; the agent compares it on receipt and silently drops
  mismatches — a third party reachable to the broker cannot
  impersonate the control plane.
- Broker network exposure (#26): the NATS broker is reverse-proxied
  by Caddy at `wss://nats.${ADMIN_DOMAIN}` with HTTP basicauth in
  front of the WebSocket upgrade, using the shared
  [[Worker basicauth]] pair. The internal broker keeps anonymous
  auth on the docker network. Same model exposes vmsingle's
  remote-write endpoint at `vm.${ADMIN_DOMAIN}/api/v1/write`. Rationale
  and rejected alternatives recorded in
  [ADR-0001](docs/adr/0001-nats-broker-exposure.md); per-instance
  basicauth migration tracked in #27.

**Management-plane deployment.** `docker-compose.yml` at the repo root
brings up the seven-service management plane on the VPS:
`caddy` (TLS reverse proxy on `${ADMIN_DOMAIN}`, ACME-issued cert,
ACME state on a named volume), `postgres` (named volume, healthcheck),
`nats` (internal-only — no host port until NATS connection-level auth
lands), `axis-control` (image from GHCR, wired to `postgres` + `nats`
by service DNS, no host port — caddy is the only ingress),
`vmsingle` (VictoriaMetrics single-node, named volume),
`grafana` (admin password from env, named volume), and
`backup` (image from GHCR, daily pg_dump + vmsingle snapshot uploaded
to S3-compatible storage — see "Backup" below). Operator workflow:
`cp .env.example .env`, fill the required secrets, `docker compose
up -d`. Verified by `tests/test_production_compose.py`: static checks
(marker `production_compose`) parse the rendered config and assert
each service's image / volumes / ports / wiring; the slower
`production_compose_integration` tests bring the stack up, exercise
`caddy → axis-control:/healthz` plus the operator-facing Caddyfile
behaviours (basicauth gate, agent-registration bypass, grafana
subdomain), and confirm postgres data survives `down && up`. Grafana
dashboard / datasource provisioning remains a follow-up.

**Operator-facing Caddyfile** (#19) layers three protections on the
admin domain:

- **Grafana on `${GRAFANA_DOMAIN}`** — defaults to
  `grafana.${ADMIN_DOMAIN}` via compose, reverse-proxied to
  `grafana:3000`. Subdomain rather than path prefix sidesteps the
  `GF_SERVER_ROOT_URL` / `serve_from_sub_path` Grafana gotcha.
- **Basicauth on the admin API** — `{$BASICAUTH_USER}` /
  `{$BASICAUTH_HASH}` from the host `.env`. Scope is everything on
  the admin domain *except* `POST /api/instances` (token-gated by the
  app — agents must register without operator creds) and `/healthz`
  (docker healthcheck + external monitors). Hash is bcrypt, generated
  with `caddy hash-password`; the `$` characters in the hash MUST be
  doubled to `$$` in the `.env` file or compose interpolates them
  away (recipe + warning in `.env.example`).
- **IP allow-list on the destructive commands path** —
  `POST /api/instances/*/commands` additionally requires the client
  IP to fall inside `{$ADMIN_ALLOW_CIDRS}` (space-separated CIDRs,
  default `0.0.0.0/0`). 403 to anything outside even with valid
  basicauth.

Long-poll-safe / SSE-friendly tweaks (no buffering, raised
read-timeout for streamed responses) remain a follow-up — the
existing `reverse_proxy` defaults are fine until a streaming endpoint
ships on the control plane. Static checks in
`tests/test_caddyfile.py`; behavioural checks in
`tests/test_production_compose.py::test_operator_facing_caddyfile_behaviors`.

**Backup.** The `backup` sidecar (#18) runs a cron loop on the
management VPS — default schedule `0 2 * * *` UTC, override via
`AXIS_BACKUP_CRON`. Each tick: pg_dumps the control DB over the
docker network (creds reused from the same `POSTGRES_*` env the
database itself uses, so they cannot drift), asks `vmsingle` to take
a snapshot via `POST /snapshot/create`, tars the snapshot dir from
the read-mounted `axis_vmsingle_data` volume, then uploads both
artifacts to an S3-compatible bucket via `aws s3 cp --endpoint-url`.
Bucket target in production is Timeweb Cloud S3
(`https://s3.timeweb.cloud`); any S3-compatible endpoint works. A
local rolling buffer on a dedicated `axis_backup_data` named volume
keeps `AXIS_BACKUP_LOCAL_RETENTION_DAYS` (default 7) days of
snapshots so a fat-fingered `docker compose down -v` is recoverable
without a remote pull. Remote retention is a bucket lifecycle rule
the operator sets on Timeweb — the image deliberately has no S3
delete permission. Encryption is delegated to the bucket's at-rest
encryption; a follow-up can layer `age`/`sops` if the threat model
shifts. Image source at `packages/backup/{Dockerfile,backup.sh}`,
published to GHCR as `ghcr.io/axisaiblr/axis-backup`. Restore is a
manual flow documented in `.env.example`; an automated
restore-roundtrip integration test is a follow-up.

## What's in motion (open issues)

Live on GitHub at <https://github.com/axisaiblr/axis-control/issues>.
Currently in `ready-for-agent`: **#26 — expose NATS broker + vmsingle
remote-write to remote workers** (design recorded in
[ADR-0001](docs/adr/0001-nats-broker-exposure.md), agent brief in the
issue comments). Blocks the first real worker deploy.

## What's not yet planned in detail (roadmap)

Filed as `needs-triage` issues so they don't get forgotten, but each
needs its own design conversation before becoming actionable:

- **Admin UI** — HTMX pages over the existing API (#9).
- **Custom per-project metrics** — vmagent on each worker scrapes
  project metrics, ships to vmsingle on the management VPS; Grafana
  dashboards (#10). Network exposure + auth on the remote-write path
  split out to #26.
- **Grafana dashboard / datasource provisioning** (#20).
- **Per-instance NATS broker credential** — migrate from the shared
  `WORKER_BASICAUTH_*` to a per-agent pair pushed into Caddy admin
  API at registration (#27). Builds on #26.
- **Cross-stack ingress extension** (#30) — generic mechanism for
  sibling compose stacks on the same VPS (`axis-infisical`, future
  admin UIs) to publish a subdomain through axis-control's Caddy
  without a coordinated PR on this repo. Implemented via
  [[Caddy extras volume]]. Hot-reload watcher and per-fragment
  basicauth orchestration are open follow-ups, not blockers — see
  [axis-infisical ADR-0002](https://github.com/axisaiblr/axis-infisical/blob/main/docs/adr/0002-caddy-extras-import-pattern.md)
  Consequences for the full list.

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

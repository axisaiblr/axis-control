# axis-control

Control plane for AXIS AI projects (text-assistant, voice-assistant, ...).

Runs on a single management VPS. Provides:

- **Admin Panel** — FastAPI + HTMX + Tailwind UI for operators.
- **Command dispatch** — disable/enable any registered worker instance via NATS.
- **Metrics** — VictoriaMetrics (vmsingle) + Grafana for per-project custom metrics.
- **Instance registry** — Postgres-backed inventory of every running deploy.

Workers are separate VPSes running their own `docker compose` stacks (text-assistant,
voice-assistant, ...) plus an `axis-agent` sidecar that connects outbound to NATS on
the management VPS and executes `docker compose stop/start` on command.

## Repository layout

```
packages/
├── shared/   # NATS message schemas (used by both control and agent)
├── control/  # FastAPI admin app + API
└── agent/    # Worker-side sidecar that listens to NATS commands
```

## Local quickstart

You need: `uv` (Python package manager) and Docker Desktop.

```powershell
# 1. Install Python deps into a venv (.venv/)
uv sync --all-packages

# 2. Bring up Postgres + NATS for development
docker compose -f docker-compose.dev.yml up -d

# 3. Configure environment (one-time)
copy .env.example .env

# 4. Run the control plane (port 8000)
uv run axis-control
```

In a second terminal, start the agent — it registers itself with the
control plane on first start and remembers the assigned id afterwards.
No copy-paste required.

```powershell
uv run axis-agent
# axis-agent starting project=text-assistant hostname=dev-laptop mode=logging
# no persisted identity at ...\axis-agent\instance.json; registering with control plane
# registered instance_id=<UUID>; persisted to ...\axis-agent\instance.json
# connected to NATS at nats://127.0.0.1:4222
# subscribed; waiting for commands on commands.<UUID>
```

Grab the assigned id from the agent log (or `GET /api/instances`), then
trigger a disable:

```powershell
curl -X POST http://127.0.0.1:8000/api/instances/<UUID>/commands `
  -H "content-type: application/json" `
  --data '{"type":"disable"}'
```

To force a clean re-registration (deliberately get a new UUID), run
`uv run axis-agent --reset-identity` — it deletes the cached
`instance.json` before connecting.

You'll see the agent log a `[dry-run] would: docker compose stop`, then the
control plane status subscriber finalises the command. Verify:

```powershell
curl http://127.0.0.1:8000/api/commands/<COMMAND_ID>
# {"status":"completed",...}

curl http://127.0.0.1:8000/api/instances/<UUID>
# {"status":"disabled",...}
```

To run against a real worker stack, set `AXIS_AGENT_COMPOSE_MODE=docker` and
`AXIS_AGENT_COMPOSE_FILE=<path>` — the agent will call `docker compose -f <path>
stop/start` for real.

## Tests

```powershell
uv run pytest
```

Spins up Postgres and NATS containers via testcontainers. ~8 seconds for the
full suite.

## Status

Pre-alpha. End-to-end disable loop works (4 integration tests passing); UI,
heartbeat, metrics, and authentication still to land.

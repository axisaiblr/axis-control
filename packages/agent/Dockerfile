# syntax=docker/dockerfile:1.7
#
# axis-agent runtime image.
#
# Build context: the repository root.
#   docker build -f packages/agent/Dockerfile -t axis-agent .
#
# Runs as a sidecar inside the worker compose stack. The agent shells
# out to `docker compose` against the worker host, so the runtime
# stage carries the Docker CLI + compose plugin and expects the host
# socket to be mounted at /var/run/docker.sock.

# --- builder ---------------------------------------------------------

FROM ghcr.io/astral-sh/uv:0.5.7-python3.12-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/shared/pyproject.toml packages/shared/
COPY packages/control/pyproject.toml packages/control/
COPY packages/agent/pyproject.toml packages/agent/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package axis-agent

COPY packages/shared/src packages/shared/src
COPY packages/agent/src packages/agent/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --package axis-agent

# --- runtime ---------------------------------------------------------

FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Docker CLI + compose v2 plugin from Docker's official Debian repo.
# The agent shells out to `docker compose -f <file> stop/start` so both
# are required. We install only the CLI (not the daemon) — the daemon
# lives on the worker host and the agent reaches it via the bind-mounted
# /var/run/docker.sock.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin && \
    apt-get purge -y --auto-remove curl gnupg && \
    rm -rf /var/lib/apt/lists/*

# Non-root account for the agent process. Note: the host's docker
# socket may be owned by a different gid; either remap with `--group-add`
# at run-time or run the container with `--user root` if the worker's
# socket permissions require it. We keep the image least-privileged
# by default.
RUN groupadd --system --gid 1000 axis && \
    useradd  --system --uid 1000 --gid axis --create-home --home-dir /home/axis axis

COPY --from=builder --chown=axis:axis /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER axis

ENTRYPOINT ["axis-agent"]

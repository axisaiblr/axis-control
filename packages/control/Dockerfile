# syntax=docker/dockerfile:1.7
#
# axis-control runtime image.
#
# Build context: the repository root (uv workspace lives there).
#   docker build -f packages/control/Dockerfile -t axis-control .
#
# Two-stage build:
#   1. builder  — uv image, syncs the workspace into /app/.venv.
#   2. runtime  — slim Debian + Python 3.12, only the populated venv.
#
# The runtime stage carries no source tree and no build tooling: only
# the resolved virtualenv plus the `axis-control` console script on
# PATH.

# --- builder ---------------------------------------------------------

FROM ghcr.io/astral-sh/uv:0.5.7-python3.12-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency-only layer: every workspace member's pyproject.toml plus the
# root lockfile, so uv can resolve the workspace without any sources yet.
# Cached aggressively — changes only when a pyproject or the lock moves.
COPY pyproject.toml uv.lock ./
COPY packages/shared/pyproject.toml packages/shared/
COPY packages/control/pyproject.toml packages/control/
COPY packages/agent/pyproject.toml packages/agent/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package axis-control

# Now bring in the sources axis-control actually needs (itself + axis-shared)
# and install them non-editable so the venv is self-contained.
COPY packages/shared/src packages/shared/src
COPY packages/control/src packages/control/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --package axis-control

# --- runtime ---------------------------------------------------------

FROM python:3.12-slim-bookworm AS runtime

# Non-root system account. UID/GID 1000 to match the standard sidecar
# convention; remap with --user at run time if a host mount needs it.
RUN groupadd --system --gid 1000 axis && \
    useradd  --system --uid 1000 --gid axis --create-home --home-dir /home/axis axis

WORKDIR /app

# Copy the populated venv. Same Python (3.12, debian:bookworm) in both
# stages so the venv's shebangs and compiled .pyc files transplant cleanly.
COPY --from=builder --chown=axis:axis /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER axis

EXPOSE 8000

ENTRYPOINT ["axis-control"]

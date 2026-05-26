"""Structural tests for the operator-facing Caddyfile (#19).

These tests parse `caddy/Caddyfile` as text and assert specific
directives are present. They are fast (no docker, no caddy binary)
and complement:

* `test_production_compose.py` — which asserts that compose threads
  the right env vars into the caddy container.
* The integration smoke in the same file — which actually brings the
  stack up and exercises the routing.

The text-based shape is intentionally lax: tests assert on a small
number of high-signal substrings (site addresses, matcher names,
reverse_proxy targets) rather than full directive equality, so a
formatting tweak does not flag a behaviour regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = REPO_ROOT / "caddy" / "Caddyfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

pytestmark = pytest.mark.production_compose


def _caddyfile_text() -> str:
    assert CADDYFILE.exists(), f"missing {CADDYFILE}"
    return CADDYFILE.read_text(encoding="utf-8")


def test_grafana_subdomain_reverse_proxies_to_grafana_service() -> None:
    """Grafana is exposed on a dedicated subdomain (decision recorded
    in issue #19) rather than a path prefix — the path-prefix approach
    requires Grafana container env (`GF_SERVER_ROOT_URL`,
    `serve_from_sub_path`) and is known-fiddly.

    The site address comes from `{$GRAFANA_DOMAIN}` (compose defaults
    it to `grafana.${ADMIN_DOMAIN}`). The literal `grafana.{$ADMIN_DOMAIN}`
    is also accepted in case an operator prefers to inline the default.

    The site block must reverse-proxy to the `grafana` service on its
    container port (3000); without this the operator cannot reach
    dashboards once they go behind caddy."""
    text = _caddyfile_text()
    grafana_site_addr_present = (
        "{$GRAFANA_DOMAIN}" in text
        or "grafana.{$ADMIN_DOMAIN}" in text
    )
    assert grafana_site_addr_present, (
        "Caddyfile is missing a grafana site block — neither "
        "`{$GRAFANA_DOMAIN}` nor `grafana.{$ADMIN_DOMAIN}` appears (#19)"
    )
    assert "reverse_proxy grafana:3000" in text, (
        "grafana site block must reverse_proxy to `grafana:3000` (the "
        "container port the grafana image listens on by default)"
    )


def test_admin_api_is_basicauth_gated_via_env() -> None:
    """The admin API endpoints (other than registration, which is
    token-gated by the app, and /healthz, which must stay public) sit
    behind caddy `basicauth`. Credentials come from the host `.env`
    via {$BASICAUTH_USER} and {$BASICAUTH_HASH} so an operator can
    rotate them without rebuilding the image.

    Stop-gap until a full app-layer auth story lands; without it the
    GET /api/instances / POST .../commands endpoints are reachable
    from anywhere on the public internet."""
    text = _caddyfile_text()
    assert "basicauth" in text, (
        "Caddyfile is missing a `basicauth` directive — the admin API "
        "is reachable from the public internet without operator creds (#19)"
    )
    assert "{$BASICAUTH_USER}" in text, (
        "basicauth must take its username from {$BASICAUTH_USER} so the "
        "credential lives in the host .env, not the image"
    )
    assert "{$BASICAUTH_HASH}" in text, (
        "basicauth must take its password hash from {$BASICAUTH_HASH} so "
        "the credential lives in the host .env, not the image. The "
        "operator generates the hash with `caddy hash-password`."
    )


def test_commands_endpoint_has_ip_allow_list() -> None:
    """`POST /api/instances/{id}/commands` is the destructive endpoint
    — it can flip workload state on any worker in the fleet. The
    issue (#19) calls for a second factor beyond basicauth: an IP
    allow-list driven by ADMIN_ALLOW_CIDRS.

    The Caddyfile must declare a matcher that restricts the commands
    path to those CIDRs (default `0.0.0.0/0` when unset, so an
    operator who has not configured an allow-list still gets a
    working stack — basicauth alone)."""
    text = _caddyfile_text()
    # Caddy supports both the `handle <path>` shorthand and the
    # explicit `@name { path ... }` form. Accept either.
    assert "/api/instances/*/commands" in text, (
        "Caddyfile is missing a matcher for /api/instances/*/commands "
        "— the destructive commands endpoint has no second factor on "
        "top of basicauth (#19)"
    )
    # The matcher branch must enforce an IP allow-list, scoped by env.
    assert "remote_ip" in text and "{$ADMIN_ALLOW_CIDRS" in text, (
        "Caddyfile must restrict the commands endpoint with a "
        "`remote_ip` matcher driven by {$ADMIN_ALLOW_CIDRS}; without "
        "it the destructive endpoint is reachable from any IP the "
        "operator's basicauth credentials leak to"
    )


def test_env_example_documents_new_caddy_vars() -> None:
    """The variables the new operator-facing Caddyfile reads must be
    surfaced in `.env.example`, with a comment explaining how to
    populate them. Without this an operator who copies `.env.example`
    to `.env` will leave them unset, and basicauth fails closed (every
    admin-API request 401s) on first `docker compose up`."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in ("BASICAUTH_USER=", "BASICAUTH_HASH=", "ADMIN_ALLOW_CIDRS="):
        assert var in text, (
            f"{var.rstrip('=')!r} is missing from .env.example — an "
            f"operator who copies .env.example to .env will leave the "
            f"new caddy directive (#19) misconfigured"
        )
    # `caddy hash-password` is the only sane way for an operator to
    # produce BASICAUTH_HASH; document it next to the variable.
    assert "caddy hash-password" in text, (
        ".env.example must mention `caddy hash-password` so the operator "
        "knows how to populate BASICAUTH_HASH — generating a bcrypt hash "
        "by hand is error-prone"
    )


def test_healthz_is_not_basicauth_gated() -> None:
    """`/healthz` must remain publicly reachable — it is the docker
    healthcheck endpoint, external uptime monitors hit it, and caddy's
    own startup probes depend on it. Putting basicauth on the whole
    admin domain (rather than scoping to /api/*) would silently break
    every one of those paths.

    The structural shape we enforce: a dedicated `handle /healthz`
    block that does no auth — just a reverse_proxy."""
    text = _caddyfile_text()
    assert re.search(r"handle\s+/healthz\b", text), (
        "Caddyfile must declare a dedicated `handle /healthz` block — "
        "without it basicauth on /api/* either leaks (covers /healthz "
        "too) or is missing (leaves /api/* open). Be explicit."
    )

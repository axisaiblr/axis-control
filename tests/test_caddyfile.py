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


def _extract_site_block(text: str, site_addr_token: str) -> str:
    """Best-effort extraction of the body of a Caddy site-block whose
    site address contains `site_addr_token`. Returns the substring
    between the first `{` after the site address and its matching `}`.
    """
    start = text.find(site_addr_token)
    assert start != -1, f"site address token {site_addr_token!r} not found"
    # The token itself contains a `{` (Caddy's env-var syntax), so we
    # must advance past it before looking for the site-block's opening
    # brace; otherwise we end up scanning the body of the env-var
    # reference and finding nothing.
    brace_open = text.find("{", start + len(site_addr_token))
    assert brace_open != -1, (
        f"no `{{` after site address {site_addr_token!r} — Caddyfile "
        "structure is unexpected"
    )
    depth = 1
    i = brace_open + 1
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, (
        f"unbalanced braces after site address {site_addr_token!r}"
    )
    return text[brace_open + 1 : i - 1]


def test_nats_subdomain_reverse_proxies_with_worker_basicauth() -> None:
    """Slice 1 (#26): NATS WSS exposure site-block. The block must:
    - take its site address from `{$NATS_DOMAIN}` so an operator can
      override the default `nats.${ADMIN_DOMAIN}` (compose default);
    - gate the upgrade with `basicauth` against `{$WORKER_BASICAUTH_*}`
      — distinct from the operator `BASICAUTH_*` so the two audiences
      can rotate independently;
    - reverse_proxy onto the internal `nats:8443` WebSocket listener
      (config'd in `nats/nats.conf`)."""
    text = _caddyfile_text()
    assert "{$NATS_DOMAIN}" in text, (
        "Caddyfile is missing a `{$NATS_DOMAIN}` site-block — workers "
        "have no path to the NATS broker (#26)"
    )
    block = _extract_site_block(text, "{$NATS_DOMAIN}")
    assert "basicauth" in block, (
        "{$NATS_DOMAIN} site-block has no basicauth directive — the "
        "NATS WSS gateway would be reachable anonymously"
    )
    assert "{$WORKER_BASICAUTH_USER}" in block, (
        "NATS site-block basicauth must read username from "
        "{$WORKER_BASICAUTH_USER} (distinct from operator BASICAUTH_*)"
    )
    assert "{$WORKER_BASICAUTH_HASH}" in block, (
        "NATS site-block basicauth must read hash from "
        "{$WORKER_BASICAUTH_HASH}"
    )
    assert "reverse_proxy nats:8443" in block, (
        "NATS site-block must reverse_proxy to `nats:8443` — the "
        "WebSocket listener configured in nats/nats.conf. Without this "
        "pin the upgrade silently routes nowhere."
    )


def test_vmsingle_subdomain_exposes_write_endpoints_only() -> None:
    """Slice 2 (#26): vmsingle remote-write exposure site-block.
    Symmetric to the NATS site-block (same basicauth pair), but with
    one critical extra: the routes are write-only. /api/v1/query,
    /internal/*, /debug/* must NOT pass through caddy — they are for
    grafana on the internal network only."""
    text = _caddyfile_text()
    assert "{$VM_DOMAIN}" in text, (
        "Caddyfile is missing a `{$VM_DOMAIN}` site-block — workers "
        "have no path to push metrics (#26)"
    )
    block = _extract_site_block(text, "{$VM_DOMAIN}")
    assert "basicauth" in block, (
        "{$VM_DOMAIN} site-block must require basicauth"
    )
    assert "{$WORKER_BASICAUTH_USER}" in block, (
        "vmsingle site-block basicauth must read username from "
        "{$WORKER_BASICAUTH_USER}"
    )
    assert "{$WORKER_BASICAUTH_HASH}" in block, (
        "vmsingle site-block basicauth must read hash from "
        "{$WORKER_BASICAUTH_HASH}"
    )
    assert "reverse_proxy vmsingle:8428" in block, (
        "vmsingle site-block must reverse_proxy to `vmsingle:8428`"
    )
    # Write-only contract — at least one canonical Prometheus /
    # VictoriaMetrics write path must appear in a path matcher.
    write_path_present = any(
        write_path in block
        for write_path in (
            "/api/v1/write",
            "/api/v1/import",
            "/api/v2/write",
        )
    )
    assert write_path_present, (
        "vmsingle site-block must scope its reverse_proxy to write "
        "paths (/api/v1/write, /api/v1/import, /api/v2/write). "
        "Exposing /api/v1/query would leak fleet observability."
    )
    # And the query endpoint must NOT be enumerated as a passthrough.
    assert "/api/v1/query" not in block, (
        "vmsingle site-block must NOT route /api/v1/query — query "
        "endpoints stay on the internal network for grafana. A "
        "worker basicauth leak would otherwise become a fleet-wide "
        "observability leak."
    )


def test_caddyfile_imports_extras_glob() -> None:
    """#30: cross-stack ingress extension. The Caddyfile MUST top-level
    `import /etc/caddy/extras/*.caddy` so a sibling compose stack on the
    same VPS (axis-infisical first, future admin UIs next) can drop a
    site-block fragment into the shared external volume and have it
    picked up at startup / on `caddy reload`.

    Failure mode this pins: a refactor silently drops the directive and
    every downstream consumer's UI 404s. The mechanism is the contract;
    without the import there is no way for sibling stacks to extend the
    ingress without a coordinated PR on this repo.

    Empty extras dir is fine — `import` no-ops cleanly. Conflict
    detection between fragments is Caddy's job at parse time (startup
    error, not silent overwrite); not asserted here.
    """
    text = _caddyfile_text()
    assert "import /etc/caddy/extras/*.caddy" in text, (
        "Caddyfile is missing the `import /etc/caddy/extras/*.caddy` "
        "directive — sibling compose stacks (axis-infisical, future "
        "admin UIs) can no longer extend the ingress without a "
        "coordinated PR on axis-control (#30, axis-infisical ADR-0002)"
    )


def test_env_example_documents_worker_basicauth_and_subdomains() -> None:
    """Slice 9 (#26): the new operator vars must be in .env.example.
    Without this an operator who copies the file to `.env` will leave
    `WORKER_BASICAUTH_*` blank and caddy's basicauth on the NATS / VM
    sites fails closed — every worker connection 401s on first start.

    Also asserts the DNS preflight checklist: an operator needs to
    add A-records for `nats.${ADMIN_DOMAIN}` and `vm.${ADMIN_DOMAIN}`
    alongside `grafana.${ADMIN_DOMAIN}`."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in ("WORKER_BASICAUTH_USER=", "WORKER_BASICAUTH_HASH="):
        assert var in text, (
            f"{var.rstrip('=')!r} is missing from .env.example (#26). "
            f"Without it an operator copying .env.example to .env will "
            f"leave the NATS/VM caddy gates broken on first start."
        )
    # The `caddy hash-password` recipe is already mentioned for the
    # operator BASICAUTH_HASH; reuse the same wording for the worker
    # hash so an operator doesn't have to relearn the trick.
    # We assert the recipe is present somewhere in the file (covers
    # both BASICAUTH_HASH and WORKER_BASICAUTH_HASH together).
    assert "caddy hash-password" in text, (
        ".env.example must mention `caddy hash-password` so the operator "
        "knows how to generate WORKER_BASICAUTH_HASH"
    )
    # DNS preflight: explicit mention that workers go through
    # nats.${ADMIN_DOMAIN} and vm.${ADMIN_DOMAIN}.
    assert "nats." in text and "vm." in text, (
        ".env.example must document that DNS A-records for "
        "`nats.${ADMIN_DOMAIN}` and `vm.${ADMIN_DOMAIN}` are required "
        "alongside grafana — workers have no other path in"
    )

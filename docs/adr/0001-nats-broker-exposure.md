# Expose NATS broker (and vmsingle remote-write) to remote workers via Caddy WebSocket reverse-proxy

**Status:** accepted (closes #26)

PR #22 closed the envelope-level half of `#8`: every NATS message carries a per-instance `agent_token` that subscribers verify in constant time. The broker itself was left internal-only, so no remote worker could actually connect. Same shape for vmsingle remote-write once `#10` lands. This ADR records how the management VPS exposes both endpoints to workers across the public internet.

## Decision

- NATS is reached by workers over **WebSockets through Caddy** at `wss://nats.${ADMIN_DOMAIN}`. The broker container stays on the internal docker network — Caddy is the only ingress and terminates TLS. Same model for vmsingle remote-write at `vm.${ADMIN_DOMAIN}` → `/api/v1/write`.
- Auth is **HTTP basicauth in Caddy**, gating the WebSocket upgrade and the remote-write POST. Broker stays anonymous on the internal network (`no_auth_user`); vmsingle exposes only its write endpoints behind the basicauth block.
- Credentials are **one shared `WORKER_BASICAUTH_USER` / `WORKER_BASICAUTH_HASH` pair**, distinct from the admin-API basicauth (`BASICAUTH_*`) introduced in `#19`. Every worker carries the same plaintext pair in its `.env` (Infisical-delivered). Per-instance migration is tracked in `#27`.
- TLS uses Caddy's existing per-subdomain ACME HTTP-01 flow — no xcaddy custom build, no DNS-01. The operator adds A-records for `nats.${ADMIN_DOMAIN}` and `vm.${ADMIN_DOMAIN}` alongside the existing `grafana.${ADMIN_DOMAIN}`.

## Considered options

**Auth model (broker-level).**

- **A. Direct `:4222` with TLS + per-instance user/pass.** Rejected — adds a new public port outside the existing 443-only model, splits the TLS pipeline between Caddy (HTTP) and NATS (TCP/TLS), gives no real isolation gain because envelope-token from `#22` already drops impersonation at the subscriber.
- **B. NATS WSS through Caddy.** **Chosen.** Single public port, single TLS pipeline, `nats-py` supports `wss://` natively, fits the model already established by admin API and Grafana subdomain.
- **C. NATS user JWTs (operator / account / user hierarchy).** Rejected for v1 — operationally expensive (nsc tooling, JWT issuance plumbing in the registration endpoint, revocation lists), and the subject-level isolation it buys is redundant while envelope-token enforces per-instance correctness at the application layer.
- **D. mTLS, cert per agent.** Rejected for v1 — same isolation argument as C, plus cert lifecycle (issuance at registration, CRL or short-lived rotation) is the highest ops cost of the four options.

**Where the auth check lives inside option B.**

- **B.1. Caddy `basicauth` before the WebSocket upgrade.** **Chosen.** One auth stack, one place to look (`Caddyfile`), reuses the `caddy hash-password` flow already documented in `.env.example`. Broker stays anonymous on the internal network.
- **B.2. NATS native user/pass via the CONNECT frame.** Rejected for v1 — NATS reloads `authorization { users [...] }` only at process start; dynamic user management on registration would require `SIGHUP` rituals or NATS Auth Callout. Worth revisiting only if/when per-instance broker credentials become a real requirement.
- **B.3. Hybrid (Caddy basicauth + NATS users).** Rejected — defence-in-depth at the cost of two credential lifecycles, no meaningful threat-model gain over B.1 once envelope-token covers integrity.

**Credential granularity.**

- **Shared `WORKER_BASICAUTH_*` across the fleet.** **Chosen for v1.** Static Caddyfile entry, single credential delivered via Infisical, no runtime state to keep in sync. Threat model: a leaked password gives observability over plaintext envelope payloads and DoS-spam, but **not** impersonation (envelope-token gates that).
- **Per-instance basicauth pool driven by control plane via Caddy admin API.** Filed as `#27`. Deferred — operationally heavier (control plane needs to push credentials into Caddy admin API on registration and re-hydrate on startup), not needed at current fleet size, non-breaking to migrate to later because Caddy's basicauth block accepts multiple user/hash pairs concurrently.

**vmsingle remote-write endpoint.**

- **Subdomain `vm.${ADMIN_DOMAIN}`.** **Chosen.** Symmetric with `nats.${ADMIN_DOMAIN}`, lets the basicauth block sit in its own Caddy site so it cannot accidentally collide with the admin-API allow-list block from `#19`.
- **Path under the admin domain (`${ADMIN_DOMAIN}/vmwrite/*`).** Rejected — saves one ACME cert and one DNS record but breaks the "every audience gets its own subdomain" model and makes the Caddy matcher block more error-prone.

**Credential split between NATS and vmsingle.**

- **One shared `WORKER_BASICAUTH_*` used by both endpoints.** **Chosen.** Workers are a single audience with a single rotation cadence; one env var is simpler on the worker side.
- **Two separate pairs.** Rejected — only cosmetic isolation, no meaningful blast-radius reduction because the agents already hold both credentials simultaneously.

## Consequences

- All worker → mgmt traffic stays on port 443. `ufw` rules on the mgmt VPS do not change; the worker firewall stays "outbound only", consistent with the architecture invariant in [CONTEXT.md](../../CONTEXT.md).
- Caddy is now load-bearing for three external audiences (operators on the admin domain, agents on `nats.${ADMIN_DOMAIN}` + `vm.${ADMIN_DOMAIN}`, dashboards on `grafana.${ADMIN_DOMAIN}`). A misconfigured Caddyfile is a single point of failure for everything except direct database/NATS access from inside the docker network.
- Rotating `WORKER_BASICAUTH_*` requires redeploying every worker in lockstep (or accepting a window of 401s while the rolling update completes). When that pain becomes real, switch to the per-instance model in `#27`.
- NATS broker subject-level permissions are **not** enforced — a leaked basicauth credential gives a third party read access to plaintext envelopes (instance_id + command payload). Envelope-token from `#22` still blocks impersonation. If a future threat model demands subject-level enforcement on the broker, that is a separate migration to option C (NATS user JWTs).
- vmsingle's remote-write endpoint is exposed; its query and admin endpoints stay internal. Grafana keeps reading from vmsingle over the docker network.

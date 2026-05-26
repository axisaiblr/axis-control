"""Per-instance agent token: minting and constant-time verification.

An agent token is an opaque URL-safe random string minted by the
control plane when an instance is registered. The plaintext is
returned to the agent exactly once (in the registration response)
and persisted on the instance row so the control plane can both:

- verify inbound `status.<id>` / `heartbeat.<id>` messages from the
  agent (constant-time compare against the stored plaintext);
- stamp outbound `commands.<id>` messages so the agent can verify
  the request really came from the control plane (and not from a
  third party reachable to the broker).

Plaintext-at-rest is acceptable here because the DB lives on the
management VPS behind the same trust boundary as the control plane
process itself; a DB read implies the attacker is already inside
that boundary. If/when the broker grows connection-level auth, this
layer becomes defence-in-depth.
"""

from __future__ import annotations

import hmac
import secrets

# 32 random bytes encoded with URL-safe base64 → ~43 characters of
# `[A-Za-z0-9_-]`. Plenty of entropy; short enough to fit comfortably
# in a NATS message envelope.
_TOKEN_BYTES = 32


def mint_agent_token() -> str:
    """Return a fresh opaque token suitable for handing to one agent."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def verify_agent_token(*, presented: str, expected: str | None) -> bool:
    """True iff the presented token matches the expected one in
    constant time. Returns False (never raises) for missing/empty
    inputs so callers can use a single boolean branch."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)

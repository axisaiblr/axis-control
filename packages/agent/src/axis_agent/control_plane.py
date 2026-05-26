"""Thin async HTTP client for the control-plane API.

The public surface is `register(project_name, hostname) ->
RegistrationOutcome`. The client presents the shared bootstrap secret
as `Authorization: Bearer <token>` when one was supplied at construction;
the control plane rejects the call otherwise.

Retry / backoff policy is the caller's concern: this module raises
`ControlPlaneUnreachable` when the transport fails so the caller can
decide whether to retry, and `ControlPlaneError` for unexpected
non-2xx responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import httpx


class ControlPlaneError(RuntimeError):
    """Control plane returned an unexpected status."""


class ControlPlaneUnreachable(ControlPlaneError):
    """Control plane could not be reached over the network."""


@dataclass(slots=True, frozen=True)
class RegistrationOutcome:
    instance_id: UUID
    agent_token: str


class ControlPlaneClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        bootstrap_token: str | None = None,
    ) -> None:
        self._http = http
        self._bootstrap_token = bootstrap_token

    async def register(
        self, *, project_name: str, hostname: str
    ) -> RegistrationOutcome:
        payload = {"project_name": project_name, "hostname": hostname}
        headers: dict[str, str] = {}
        if self._bootstrap_token is not None:
            headers["Authorization"] = f"Bearer {self._bootstrap_token}"
        try:
            response = await self._http.post(
                "/api/instances", json=payload, headers=headers
            )
        except httpx.TransportError as exc:
            raise ControlPlaneUnreachable(
                f"control plane unreachable: {exc!r}"
            ) from exc
        if response.status_code >= 400:
            raise ControlPlaneError(
                f"control plane returned {response.status_code}: "
                f"{response.text}"
            )
        body = response.json()
        try:
            return RegistrationOutcome(
                instance_id=UUID(body["id"]),
                agent_token=body["agent_token"],
            )
        except KeyError as exc:
            raise ControlPlaneError(
                f"control plane response missing required field: {exc!r}"
            ) from exc

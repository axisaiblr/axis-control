"""Thin async HTTP client for the control-plane API.

The public surface today is just `register(project_name, hostname) ->
instance_id`. Retry / backoff policy is the caller's concern: this module
raises `ControlPlaneUnreachable` when the transport fails so the caller
can decide whether to retry, and `ControlPlaneError` for unexpected
non-2xx responses.
"""

from __future__ import annotations

from uuid import UUID

import httpx


class ControlPlaneError(RuntimeError):
    """Control plane returned an unexpected status."""


class ControlPlaneUnreachable(ControlPlaneError):
    """Control plane could not be reached over the network."""


class ControlPlaneClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def register(
        self, *, project_name: str, hostname: str
    ) -> UUID:
        payload = {"project_name": project_name, "hostname": hostname}
        try:
            response = await self._http.post(
                "/api/instances", json=payload
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
        return UUID(body["id"])

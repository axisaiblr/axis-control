from __future__ import annotations

import hmac
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from axis_control.domain.models import (
    Instance,
    Reachability,
    WorkloadState,
    reachability_of,
)


class InstanceResponse(BaseModel):
    id: UUID
    project_id: UUID
    project_name: str
    hostname: str
    workload_state: WorkloadState
    reachability: Reachability
    last_heartbeat_at: datetime | None


class RegisterInstanceResponse(InstanceResponse):
    """Response for `POST /api/instances`. Includes the plaintext
    `agent_token` exactly once — the control plane only persists the
    hash, so any subsequent fetch of the instance row (via
    `GET /api/instances/{id}`) omits this field."""

    agent_token: str


class RegisterInstanceRequest(BaseModel):
    project_name: str
    hostname: str


_INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing bootstrap registration token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _verify_bootstrap_token(
    request: Request, authorization: str | None
) -> None:
    expected = request.app.state.registration_token
    if not expected:
        raise _INVALID_TOKEN
    if authorization is None:
        raise _INVALID_TOKEN
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise _INVALID_TOKEN
    # Constant-time compare keeps the endpoint from being a timing oracle
    # for the shared bootstrap secret.
    if not hmac.compare_digest(presented, expected):
        raise _INVALID_TOKEN


router = APIRouter()


def _to_response(instance: Instance, request: Request) -> InstanceResponse:
    stale_after = request.app.state.heartbeat_stale_after
    reachability = reachability_of(
        instance.last_heartbeat_at,
        now=datetime.now(timezone.utc),
        stale_after=stale_after,
    )
    return InstanceResponse(
        id=instance.id,
        project_id=instance.project_id,
        project_name=instance.project_name,
        hostname=instance.hostname,
        workload_state=instance.workload_state,
        reachability=reachability,
        last_heartbeat_at=instance.last_heartbeat_at,
    )


@router.post(
    "/api/instances",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterInstanceResponse,
)
async def register_instance(
    payload: RegisterInstanceRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> RegisterInstanceResponse:
    _verify_bootstrap_token(request, authorization)
    service = request.app.state.registration_service
    result = await service.register(
        project_name=payload.project_name, hostname=payload.hostname
    )
    base = _to_response(result.instance, request)
    return RegisterInstanceResponse(
        **base.model_dump(), agent_token=result.agent_token
    )


@router.get("/api/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: UUID, request: Request
) -> InstanceResponse:
    repo = request.app.state.instances_repo
    instance = await repo.get(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return _to_response(instance, request)

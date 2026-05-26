from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
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


class RegisterInstanceRequest(BaseModel):
    project_name: str
    hostname: str


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
    response_model=InstanceResponse,
)
async def register_instance(
    payload: RegisterInstanceRequest, request: Request
) -> InstanceResponse:
    service = request.app.state.registration_service
    instance = await service.register(
        project_name=payload.project_name, hostname=payload.hostname
    )
    return _to_response(instance, request)


@router.get("/api/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: UUID, request: Request
) -> InstanceResponse:
    repo = request.app.state.instances_repo
    instance = await repo.get(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return _to_response(instance, request)

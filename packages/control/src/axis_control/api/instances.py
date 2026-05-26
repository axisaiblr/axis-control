from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from axis_control.domain.models import InstanceStatus


class InstanceResponse(BaseModel):
    id: UUID
    project_id: UUID
    project_name: str
    hostname: str
    status: InstanceStatus


class RegisterInstanceRequest(BaseModel):
    project_name: str
    hostname: str


router = APIRouter()


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
    return InstanceResponse(
        id=instance.id,
        project_id=instance.project_id,
        project_name=instance.project_name,
        hostname=instance.hostname,
        status=instance.status,
    )


@router.get("/api/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: UUID, request: Request
) -> InstanceResponse:
    repo = request.app.state.instances_repo
    instance = await repo.get(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return InstanceResponse(
        id=instance.id,
        project_id=instance.project_id,
        project_name=instance.project_name,
        hostname=instance.hostname,
        status=instance.status,
    )

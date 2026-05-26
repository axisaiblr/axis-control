from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from axis_control.domain.commands import (
    CommandStatus,
    CommandType,
    DeliveryHint,
)
from axis_control.services.command_dispatcher import InstanceNotRegistered


class IssueCommandRequest(BaseModel):
    type: CommandType


class CommandResponse(BaseModel):
    id: UUID
    instance_id: UUID
    type: CommandType
    status: CommandStatus
    failure_reason: str | None = None


class IssueCommandResponse(CommandResponse):
    """Response shape for `POST /api/instances/{id}/commands`.

    Adds a best-effort `delivery` hint captured at publish time. The hint
    is informational — a `no_listeners` response still persists the
    command and still arms the timeout sweeper.
    """

    delivery: DeliveryHint


router = APIRouter()


@router.post(
    "/api/instances/{instance_id}/commands",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IssueCommandResponse,
)
async def issue_command(
    instance_id: UUID,
    payload: IssueCommandRequest,
    request: Request,
) -> IssueCommandResponse:
    dispatcher = request.app.state.command_dispatcher
    try:
        result = await dispatcher.dispatch(instance_id, payload.type)
    except InstanceNotRegistered as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    command = result.command
    return IssueCommandResponse(
        id=command.id,
        instance_id=command.instance_id,
        type=command.type,
        status=command.status,
        failure_reason=command.failure_reason,
        delivery=result.delivery,
    )


@router.get("/api/commands/{command_id}", response_model=CommandResponse)
async def get_command(command_id: UUID, request: Request) -> CommandResponse:
    repo = request.app.state.commands_repo
    command = await repo.get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="command not found")
    return CommandResponse(
        id=command.id,
        instance_id=command.instance_id,
        type=command.type,
        status=command.status,
        failure_reason=command.failure_reason,
    )

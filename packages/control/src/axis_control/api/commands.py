from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from axis_control.domain.commands import CommandStatus, CommandType


class IssueCommandRequest(BaseModel):
    type: CommandType


class CommandResponse(BaseModel):
    id: UUID
    instance_id: UUID
    type: CommandType
    status: CommandStatus


router = APIRouter()


@router.post(
    "/api/instances/{instance_id}/commands",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandResponse,
)
async def issue_command(
    instance_id: UUID,
    payload: IssueCommandRequest,
    request: Request,
) -> CommandResponse:
    dispatcher = request.app.state.command_dispatcher
    command = await dispatcher.dispatch(instance_id, payload.type)
    return CommandResponse(
        id=command.id,
        instance_id=command.instance_id,
        type=command.type,
        status=command.status,
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
    )

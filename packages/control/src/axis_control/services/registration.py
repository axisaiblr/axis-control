from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from axis_control.domain.auth import mint_agent_token
from axis_control.domain.models import Instance, Project, new_instance


class ProjectsRepoPort(Protocol):
    async def find_or_create_by_name(self, name: str) -> Project: ...


class InstancesRepoPort(Protocol):
    async def save(self, instance: Instance) -> None: ...


@dataclass(slots=True, frozen=True)
class RegistrationResult:
    """The instance row that was just created, plus the plaintext
    `agent_token` returned to the caller. The same plaintext is
    persisted on the instance row so the control plane can both
    verify inbound messages from this agent and stamp outbound
    commands intended for it."""

    instance: Instance
    agent_token: str


class RegistrationService:
    def __init__(
        self,
        projects_repo: ProjectsRepoPort,
        instances_repo: InstancesRepoPort,
    ) -> None:
        self._projects_repo = projects_repo
        self._instances_repo = instances_repo

    async def register(
        self, project_name: str, hostname: str
    ) -> RegistrationResult:
        project = await self._projects_repo.find_or_create_by_name(
            project_name
        )
        token = mint_agent_token()
        instance = new_instance(
            project, hostname=hostname, agent_token=token
        )
        await self._instances_repo.save(instance)
        return RegistrationResult(instance=instance, agent_token=token)

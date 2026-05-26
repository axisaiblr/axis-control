from __future__ import annotations

from typing import Protocol

from axis_control.domain.models import Instance, Project, new_instance


class ProjectsRepoPort(Protocol):
    async def find_or_create_by_name(self, name: str) -> Project: ...


class InstancesRepoPort(Protocol):
    async def save(self, instance: Instance) -> None: ...


class RegistrationService:
    def __init__(
        self,
        projects_repo: ProjectsRepoPort,
        instances_repo: InstancesRepoPort,
    ) -> None:
        self._projects_repo = projects_repo
        self._instances_repo = instances_repo

    async def register(self, project_name: str, hostname: str) -> Instance:
        project = await self._projects_repo.find_or_create_by_name(
            project_name
        )
        instance = new_instance(project, hostname=hostname)
        await self._instances_repo.save(instance)
        return instance

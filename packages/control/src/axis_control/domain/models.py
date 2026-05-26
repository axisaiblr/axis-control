from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InstanceStatus(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    DISABLED = "disabled"


@dataclass(slots=True, frozen=True)
class Project:
    id: UUID
    name: str
    created_at: datetime


@dataclass(slots=True, frozen=True)
class Instance:
    id: UUID
    project_id: UUID
    project_name: str
    hostname: str
    status: InstanceStatus = InstanceStatus.UNKNOWN
    created_at: datetime = field(default_factory=_utcnow)


def new_project(name: str) -> Project:
    return Project(id=uuid4(), name=name, created_at=_utcnow())


def new_instance(project: Project, hostname: str) -> Instance:
    return Instance(
        id=uuid4(),
        project_id=project.id,
        project_name=project.name,
        hostname=hostname,
    )

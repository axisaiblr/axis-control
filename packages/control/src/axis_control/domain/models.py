from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID, uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkloadState(StrEnum):
    """The operator's last-expressed intent for an instance. Orthogonal
    to reachability — survives the agent going offline and is only
    changed by a successful enable/disable command."""

    UNKNOWN = "unknown"
    ENABLED = "enabled"
    DISABLED = "disabled"


class Reachability(StrEnum):
    """Derived (not stored) liveness indicator computed from
    `last_heartbeat_at` and a configurable staleness window."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


def reachability_of(
    last_heartbeat_at: datetime | None,
    *,
    now: datetime,
    stale_after: timedelta,
) -> Reachability:
    if last_heartbeat_at is None:
        return Reachability.UNKNOWN
    if now - last_heartbeat_at <= stale_after:
        return Reachability.ONLINE
    return Reachability.OFFLINE


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
    workload_state: WorkloadState = WorkloadState.UNKNOWN
    created_at: datetime = field(default_factory=_utcnow)
    last_heartbeat_at: datetime | None = None


def new_project(name: str) -> Project:
    return Project(id=uuid4(), name=name, created_at=_utcnow())


def new_instance(project: Project, hostname: str) -> Instance:
    return Instance(
        id=uuid4(),
        project_id=project.id,
        project_name=project.name,
        hostname=hostname,
    )

"""On-disk persistence of the agent's assigned instance identity.

The store is a single JSON file under a configurable state directory.
It knows nothing about HTTP, NATS, or settings — it is a typed
key-value with one record.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

IDENTITY_FILENAME = "instance.json"


@dataclass(slots=True, frozen=True)
class AgentIdentity:
    instance_id: UUID
    project_name: str
    hostname: str
    registered_at: datetime


class AgentIdentityStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._path = state_dir / IDENTITY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AgentIdentity | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        data = json.loads(raw)
        return AgentIdentity(
            instance_id=UUID(data["instance_id"]),
            project_name=data["project_name"],
            hostname=data["hostname"],
            registered_at=datetime.fromisoformat(data["registered_at"]),
        )

    def save(self, identity: AgentIdentity) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(identity)
        payload["instance_id"] = str(identity.instance_id)
        payload["registered_at"] = identity.registered_at.isoformat()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

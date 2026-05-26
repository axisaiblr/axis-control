"""Static checks on the backup sidecar image (#18).

The Dockerfile + entrypoint script for the `backup` service must live
in the repo so the GHCR publish workflow can build it, and the
entrypoint must consume every `AXIS_BACKUP_*` env var the production
compose stamps into the container — drift between the two would
silently produce empty (or no) backups.

A full image-build + S3 roundtrip test belongs behind a slower marker;
this file stays cheap so it runs in the default loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "packages" / "backup" / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "packages" / "backup" / "backup.sh"

pytestmark = pytest.mark.production_compose


def test_backup_dockerfile_exists() -> None:
    """The production compose references ghcr.io/axisaiblr/axis-backup
    — the source for that image must live in the repo so the GHCR
    publish workflow can build it."""
    assert DOCKERFILE.exists(), (
        f"missing {DOCKERFILE} — production compose references "
        "ghcr.io/axisaiblr/axis-backup but no Dockerfile defines it"
    )


def test_backup_entrypoint_uses_required_axis_env() -> None:
    """The entrypoint must read the AXIS_BACKUP_* env vars the
    production compose hands it. If a var is added to compose but not
    consumed here (or removed from here but still stamped by compose)
    backups silently fall back to defaults — or to nothing at all."""
    assert ENTRYPOINT.exists(), f"missing {ENTRYPOINT}"
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for var in (
        "AXIS_BACKUP_POSTGRES_HOST",
        "AXIS_BACKUP_POSTGRES_USER",
        "AXIS_BACKUP_POSTGRES_PASSWORD",
        "AXIS_BACKUP_POSTGRES_DB",
        "AXIS_BACKUP_S3_ENDPOINT",
        "AXIS_BACKUP_S3_BUCKET",
        "AXIS_BACKUP_S3_ACCESS_KEY_ID",
        "AXIS_BACKUP_S3_SECRET_ACCESS_KEY",
        "AXIS_BACKUP_VMSINGLE_URL",
        "AXIS_BACKUP_CRON",
        "AXIS_BACKUP_LOCAL_RETENTION_DAYS",
    ):
        assert var in text, (
            f"backup entrypoint does not reference {var} — the production "
            "compose threads it in but the script silently ignores it"
        )

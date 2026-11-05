"""Inspect optional capabilities without contacting Git or a cloud service."""

from __future__ import annotations

import shutil
from typing import Any

from dagoberd.workspaces import WorkspaceRegistry


def workspace_status(registry: WorkspaceRegistry, selector: str) -> dict[str, Any]:
    entry = registry.entry(selector)
    loaded = registry.load(selector)
    return {
        "workspace_id": entry.id,
        "valid": True,
        "database_exists": loaded.config.database.path.is_file(),
        "storage": "local",
        "git": {
            "configured": (loaded.project_path.parent / ".git").exists(),
            "available": shutil.which("git") is not None,
        },
        "synchronization": {
            "backend": "snapshot-folder" if entry.sync_root else None,
            "root": str(entry.sync_root) if entry.sync_root else None,
        },
        "cloud_workspace": str(loaded.config.cloud.workspace)
        if loaded.config.cloud.workspace
        else None,
    }

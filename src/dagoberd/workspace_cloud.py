from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import tomlkit

from dagoberd.ledger.database import LedgerDatabase
from dagoberd.services.portability import PortabilityService
from dagoberd.services.restore_paths import rebase_archive_references
from dagoberd.workspace_lock import file_lock
from dagoberd.workspaces import WorkspaceRegistry, atomic_text, overlaps, workspace_operation


class WorkspaceCloud:
    def __init__(self, registry: WorkspaceRegistry, selector: str) -> None:
        self.registry = registry
        self.entry = registry.entry(selector)
        self.loaded = registry.load(selector)

    def _plan(self, cloud_root: str) -> dict[str, Any]:
        root = Path(cloud_root).expanduser().resolve()
        target = root / "workspaces" / self.entry.id
        if target.resolve() != target:
            raise ValueError("Cloud workspace namespace cannot be redirected through links")
        if target.exists():
            raise ValueError("New cloud workspace destination must not exist")
        config = self.loaded.config
        if any(
            overlaps(target, path)
            for path in (
                config.database.path,
                config.documents.storage_dir,
                config.reporting.output_dir,
                self.loaded.project_path.parent / ".account-eval",
            )
        ):
            raise ValueError("New cloud destination overlaps existing workspace storage")
        for entry in self.registry.read().workspaces:
            if entry.id != self.entry.id:
                other = self.registry.load(entry.id)
                for path in (
                    other.config.database.path,
                    other.config.documents.storage_dir,
                    other.config.reporting.output_dir,
                ):
                    if overlaps(target, path):
                        raise ValueError("Cloud destination overlaps another workspace")
        files: list[dict[str, Any]] = []
        portability = PortabilityService(
            config.database.path,
            config.documents.storage_dir,
            config.reporting.output_dir,
            self.loaded.project_path,
        )
        report_paths = set(portability._report_paths())
        for kind, source in (
            ("documents", config.documents.storage_dir),
            ("reports", config.reporting.output_dir),
        ):
            if not source.exists():
                continue
            for path in sorted(source.rglob("*")):
                if (
                    path.is_symlink()
                    or path.is_junction()
                    or not path.resolve().is_relative_to(source.resolve())
                ):
                    raise ValueError("Cloud migration cannot follow links outside managed storage")
                if not path.is_file():
                    continue
                if kind == "reports" and path.resolve() not in report_paths:
                    continue
                if any(part.startswith(".") for part in path.relative_to(source).parts):
                    continue
                if kind == "reports" and (
                    path.resolve() == config.database.path.resolve()
                    or path.resolve().is_relative_to(config.documents.storage_dir.resolve())
                    or path.name.endswith((".lock", "-wal", "-shm", "-journal"))
                ):
                    continue
                files.append(
                    {
                        "source": str(path.resolve()),
                        "relative": f"{kind}/{path.relative_to(source).as_posix()}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        configs = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in (self.loaded.project_path, self.loaded.local_path)
        }
        return {
            "workspace_id": self.entry.id,
            "cloud_root": str(root),
            "destination": str(target),
            "files": files,
            "configs": configs,
            "ledger": {
                suffix: hashlib.sha256(
                    Path(f"{config.database.path}{suffix}").read_bytes()
                ).hexdigest()
                if Path(f"{config.database.path}{suffix}").is_file()
                else None
                for suffix in ("", "-wal")
            },
        }

    def preview(self, cloud_root: str) -> dict[str, Any]:
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            plan = self._plan(cloud_root)
            fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
            identifier = str(uuid4())
            atomic_text(
                self.registry.path.parent / "cloud-previews" / f"{identifier}.json",
                json.dumps({"plan": plan, "fingerprint": fingerprint}),
            )
            return {"preview_id": identifier, "fingerprint": fingerprint, **plan}

    def run(self, preview_id: str, fingerprint: str, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("Cloud migration requires local confirmation with --yes")
        path = self.registry.path.parent / "cloud-previews" / f"{UUID(preview_id)}.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        plan = saved["plan"]
        if plan["workspace_id"] != self.entry.id:
            raise ValueError("Cloud preview belongs to another workspace")
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            current = self._plan(plan["cloud_root"])
            digest = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
            if fingerprint != saved["fingerprint"] or fingerprint != digest:
                raise ValueError("Workspace files changed; create a new cloud preview")
            target = Path(plan["destination"])
            target.mkdir(parents=True, exist_ok=False)
            for item in plan["files"]:
                destination = target / item["relative"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item["source"], destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Cloud copy verification failed; original files are preserved")
            project = tomlkit.parse(self.loaded.project_path.read_text(encoding="utf-8"))
            local = (
                tomlkit.parse(self.loaded.local_path.read_text(encoding="utf-8"))
                if self.loaded.local_path.exists()
                else tomlkit.document()
            )
            for document in (project, local):
                document.pop("cloud", None)
                for section, key in (("documents", "storage_dir"), ("reporting", "output_dir")):
                    if section in document:
                        document[section].pop(key, None)
            local["cloud"] = {"root": plan["cloud_root"]}
            project_before = self.loaded.project_path.read_text(encoding="utf-8")
            local_before = (
                self.loaded.local_path.read_text(encoding="utf-8")
                if self.loaded.local_path.exists()
                else None
            )
            recovery = (
                self.loaded.project_path.parent / ".account-eval" / "cloud-recovery" / str(uuid4())
            )
            recovery.mkdir(parents=True, exist_ok=False)
            database = LedgerDatabase(self.loaded.config.database.path)
            database.initialize()
            backup = database.backup(recovery / "previous.sqlite3")
            prepared = recovery / "prepared.sqlite3"
            shutil.copyfile(backup, prepared)
            rebase_archive_references(
                LedgerDatabase(prepared),
                {
                    "documents": str(self.loaded.config.documents.storage_dir),
                    "output": str(self.loaded.config.reporting.output_dir),
                },
                [(item["relative"], target / item["relative"]) for item in plan["files"]],
            )
            self.registry.refresh_storage(self.entry.id)
            journal = self.loaded.project_path.parent / ".account-eval" / "cloud-migration.json"
            atomic_text(
                journal,
                json.dumps(
                    {
                        "workspace_id": self.entry.id,
                        "database": str(database.path),
                        "backup": str(backup),
                        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                        "project_before": project_before,
                        "local_before": local_before,
                    }
                ),
            )
            try:
                database.restore(prepared)
                atomic_text(self.loaded.project_path, tomlkit.dumps(project))
                atomic_text(self.loaded.local_path, tomlkit.dumps(local))
                self.registry.refresh_storage(self.entry.id)
            except Exception:
                atomic_text(self.loaded.project_path, project_before)
                if local_before is not None:
                    atomic_text(self.loaded.local_path, local_before)
                else:
                    self.loaded.local_path.unlink(missing_ok=True)
                database.restore(backup)
                journal.unlink()
                raise
            journal.unlink()
            path.unlink()
            return {
                "workspace_id": self.entry.id,
                "cloud_workspace": str(target),
                "copied_files": len(plan["files"]),
                "original_files_preserved": True,
            }


def recover_cloud(registry: WorkspaceRegistry, selector: str, *, yes: bool) -> dict[str, Any]:
    if not yes:
        raise ValueError("Cloud recovery requires local confirmation with --yes")
    entry = registry.entry(selector)
    metadata = entry.config_path.parent / ".account-eval"
    journal = metadata / "cloud-migration.json"
    with file_lock(metadata / "workspace.lock", exclusive=True):
        record = json.loads(journal.read_text(encoding="utf-8"))
        database = Path(record["database"]).resolve()
        backup = Path(record["backup"]).resolve()
        if record["workspace_id"] != entry.id or database != entry.storage_paths[0].resolve():
            raise ValueError("Cloud recovery journal does not match the registered workspace")
        if (
            not backup.is_relative_to((metadata / "cloud-recovery").resolve())
            or hashlib.sha256(backup.read_bytes()).hexdigest() != record["backup_sha256"]
        ):
            raise ValueError("Cloud recovery backup is missing or changed")
        with file_lock(database.with_suffix(".workspace.lock"), exclusive=True):
            with closing(sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)) as connection:
                identity = connection.execute(
                    "SELECT workspace_id FROM ledger_workspace_identity WHERE singleton=1"
                ).fetchone()
                if identity is None or identity[0] != entry.id:
                    raise ValueError("Cloud recovery backup belongs to another workspace")
            LedgerDatabase(database).restore(backup)
            atomic_text(entry.config_path, record["project_before"])
            local = entry.config_path.with_name("account-eval.local.toml")
            if record["local_before"] is None:
                local.unlink(missing_ok=True)
            else:
                atomic_text(local, record["local_before"])
            registry.refresh_storage(entry.id)
            journal.unlink()
    return {"workspace_id": entry.id, "recovered": True, "cloud_copies_preserved": True}

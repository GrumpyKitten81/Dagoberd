"""Explicit snapshot exchange through a user-managed synchronized folder.

Immutable revisions expose cloud-provider races as branches rather than overwrites.
There is deliberately no background writer or live SQLite replication.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

import tomlkit
from pydantic import Field

from dagoberd.config import DEFAULT_CONFIG_TOML, StrictModel
from dagoberd.safe_files import prepared_new_file
from dagoberd.services.portability import PortabilityService
from dagoberd.workspaces import (
    WorkspaceRegistry,
    atomic_text,
    overlaps,
    storage_paths,
    workspace_operation,
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def validate_snapshot_members(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name.lower())
        if any(
            part == ".env"
            or part.startswith(".env.")
            or part.startswith(".account-")
            or ".local.toml" in part
            or part in {"dagoberd.local.toml", "workspaces.toml"}
            for part in path.parts
        ) or path.suffix in {".pem", ".key", ".pfx", ".p12"}:
            raise ValueError(
                "Snapshots cannot include credential or machine-local configuration files"
            )


class SnapshotRecord(StrictModel):
    format: Literal["dagoberd-snapshot-v1"]
    revision: UUID
    parent: UUID | None
    workspace_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def read_record(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 65536:
        raise ValueError("Snapshot revision metadata is oversized")
    record = SnapshotRecord.model_validate_json(path.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    if (
        record["revision"] != path.stem
        or record["workspace_id"] != path.parent.name
        or path.parent.parent.name != "workspaces"
    ):
        raise ValueError("Invalid snapshot revision")
    return record


def inspect_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    record = read_record(source)
    bundle = source.with_suffix(".aex")
    if bundle.is_symlink() or file_hash(bundle) != record["sha256"]:
        raise ValueError("Snapshot is incomplete or its checksum changed")
    with tempfile.TemporaryDirectory(prefix="dagoberd-snapshot-inspect-") as directory:
        root = Path(directory)
        service = PortabilityService(root / "ledger.db", root / "docs", root, root / "config")
        service.verify(bundle)
        ledger = root / "ledger.db"
        with zipfile.ZipFile(bundle) as archive:
            validate_snapshot_members(archive.namelist())
            ledger.write_bytes(archive.read("ledger/ledger.sqlite3"))
        with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as connection:
            identity = connection.execute(
                "SELECT workspace_id FROM ledger_workspace_identity WHERE singleton=1"
            ).fetchone()
            if not identity or identity[0] != record["workspace_id"]:
                raise ValueError("Snapshot ledger belongs to another workspace")
    return {**record, "snapshot": str(source), "fingerprint": digest(record)}


class WorkspaceSync:
    def __init__(self, registry: WorkspaceRegistry, selector: str) -> None:
        self.registry = registry
        self.entry = registry.entry(selector)
        self.loaded = registry.load(selector)
        config = self.loaded.config
        self.portability = PortabilityService(
            config.database.path,
            config.documents.storage_dir,
            config.reporting.output_dir,
            self.loaded.project_path,
        )
        self.state_path = self.loaded.project_path.parent / ".dagoberd" / "sync-state.json"

    def configure(self, root: str | None, *, yes: bool) -> dict[str, Any]:
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            return self._configure(root, yes=yes)

    def _configure(self, root: str | None, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("Confirm private snapshot storage or disconnection with --yes")
        target = Path(root).expanduser().resolve() if root else None
        if target:
            namespace = target / "workspaces" / self.entry.id
            if namespace.resolve() != namespace:
                raise ValueError("Snapshot namespace cannot be redirected through links")
            for entry in self.registry.read().workspaces:
                loaded = self.registry.load(entry.id)
                if any(
                    overlaps(namespace, path)
                    for path in (loaded.project_path.parent, *storage_paths(loaded))
                ):
                    raise ValueError("Snapshot folder overlaps local workspace storage")
                if loaded.project_path.parent.resolve().is_relative_to(
                    target
                ) or loaded.config.database.path.resolve().is_relative_to(target):
                    raise ValueError("Keep active workspaces outside the synchronized root")
            target.mkdir(parents=True, exist_ok=True)
        self.registry.configure_sync(self.entry.id, target)
        return WorkspaceSync(self.registry, self.entry.id).status()

    def _namespace(self) -> Path:
        root = self.registry.entry(self.entry.id).sync_root
        if root is None:
            raise ValueError("No snapshot folder configured; local operations remain available")
        if not root.is_dir():
            raise ValueError("Snapshot folder is unavailable; local operations remain available")
        target = root / "workspaces" / self.entry.id
        if target.resolve() != target:
            raise ValueError("Snapshot namespace cannot be redirected through links")
        return target

    def _head(self) -> dict[str, Any] | None:
        namespace = self._namespace()
        records = {}
        for path in namespace.glob("*.json"):
            if path.is_symlink():
                raise ValueError("Snapshot revisions cannot be symbolic links")
            record = read_record(path)
            if not path.with_suffix(".aex").is_file():
                raise ValueError("Snapshot synchronization is incomplete; retry after replication")
            records[record["revision"]] = record
        if not records:
            return None
        parents = {record["parent"] for record in records.values() if record["parent"]}
        heads = set(records) - parents
        roots = [record for record in records.values() if record["parent"] is None]
        if parents - set(records):
            raise ValueError("Snapshot ancestry is incomplete; wait for folder synchronization")
        if len(heads) != 1 or len(roots) != 1:
            raise ValueError("Conflicting snapshot branches; preserve both and resolve explicitly")
        head = records[heads.pop()]
        visited = set()
        current: dict[str, Any] | None = head
        while current:
            if current["revision"] in visited:
                raise ValueError("Invalid snapshot ancestry")
            visited.add(current["revision"])
            current = records.get(current["parent"])
        if len(visited) != len(records):
            raise ValueError("Conflicting or invalid snapshot ancestry")
        return head

    def _state(self) -> dict[str, Any]:
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        return state if state.get("namespace") == str(self._namespace()) else {}

    def _local_digest(self) -> str:
        database = self.loaded.config.database.path
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            ledger_hash = hashlib.sha256()
            for line in connection.iterdump():
                ledger_hash.update(line.encode())
                ledger_hash.update(b"\n")
            ledger = ledger_hash.hexdigest()
        members = self.portability._managed_members()
        return digest(
            {
                "ledger": ledger,
                "files": {name: file_hash(path) for name, path, _kind in members},
                "config": file_hash(self.loaded.project_path),
                "local_config": file_hash(self.loaded.local_path)
                if self.loaded.local_path.is_file()
                else None,
            }
        )

    def _save_state(self, revision: str) -> None:
        atomic_text(
            self.state_path,
            json.dumps(
                {
                    "namespace": str(self._namespace()),
                    "revision": revision,
                    "local_digest": self._local_digest(),
                }
            ),
        )

    def status(self) -> dict[str, Any]:
        self.entry = self.registry.entry(self.entry.id)
        result: dict[str, Any] = {
            "workspace_id": self.entry.id,
            "backend": "snapshot-folder" if self.entry.sync_root else None,
            "root": str(self.entry.sync_root) if self.entry.sync_root else None,
            "automatic": False,
        }
        if self.entry.sync_root:
            try:
                head = self._head()
                result.update(
                    remote_revision=head["revision"] if head else None,
                    local_revision=self._state().get("revision"),
                    available=True,
                )
            except (OSError, ValueError, KeyError) as error:
                result.update(available=False, error=str(error))
        return result

    def _plan(self, action: Literal["publish", "restore"]) -> dict[str, Any]:
        head, state = self._head(), self._state()
        revision = head["revision"] if head else None
        local = self._local_digest()
        if action == "publish" and revision != state.get("revision"):
            raise ValueError("Remote snapshots changed; restore the reviewed revision first")
        if head:
            inspect_snapshot(self._namespace() / f"{revision}.json")
        if action == "restore":
            if not head:
                raise ValueError("No complete snapshot is available")
            if not state or local != state.get("local_digest"):
                raise ValueError("Local changes conflict with restore; preserve them separately")
        return {
            "workspace_id": self.entry.id,
            "action": action,
            "namespace": str(self._namespace()),
            "head": head,
            "local_digest": local,
            "base": state.get("revision"),
        }

    def preview(self, action: Literal["publish", "restore"]) -> dict[str, Any]:
        if action not in {"publish", "restore"}:
            raise ValueError("Unknown snapshot action")
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            plan = self._plan(action)
            identifier = str(uuid4())
            fingerprint = digest(plan)
            atomic_text(
                self.state_path.parent / "sync-previews" / f"{identifier}.json",
                json.dumps({"plan": plan, "fingerprint": fingerprint}),
            )
        return {"preview_id": identifier, "fingerprint": fingerprint, **plan}

    def run(self, preview_id: str, fingerprint: str, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("Snapshot exchange requires local confirmation with --yes")
        path = self.state_path.parent / "sync-previews" / f"{UUID(preview_id)}.json"
        preview = json.loads(path.read_text())
        plan = preview["plan"]
        if plan["workspace_id"] != self.entry.id:
            raise ValueError("Snapshot preview belongs to another workspace")
        namespace = self._namespace()
        namespace.mkdir(parents=True, exist_ok=True)
        from dagoberd.workspace_lock import file_lock

        with (
            workspace_operation(self.loaded, self.registry, exclusive=True),
            file_lock(namespace / ".exchange.lock", exclusive=True),
        ):
            current = self._plan(plan["action"])
            if fingerprint != preview["fingerprint"] or fingerprint != digest(current):
                raise ValueError("Workspace or snapshots changed; create a new preview")
            path.unlink()
            if plan["action"] == "publish":
                revision = str(uuid4())
                bundle = namespace / f"{revision}.aex"
                with tempfile.TemporaryDirectory(prefix="dagoberd-snapshot-publish-") as directory:
                    local = Path(directory) / "snapshot.aex"
                    self.portability.export(local)
                    with zipfile.ZipFile(local) as archive:
                        validate_snapshot_members(archive.namelist())
                    if self._local_digest() != plan["local_digest"]:
                        raise ValueError("Workspace changed during snapshot preparation; retry")
                    checksum = file_hash(local)
                    with prepared_new_file(bundle) as staged:
                        shutil.copyfile(local, staged)
                    if file_hash(bundle) != checksum:
                        raise ValueError("Snapshot changed during publication; retry")
                record = {
                    "format": "dagoberd-snapshot-v1",
                    "revision": revision,
                    "parent": plan["head"]["revision"] if plan["head"] else None,
                    "workspace_id": self.entry.id,
                    "sha256": checksum,
                }
                with prepared_new_file(namespace / f"{revision}.json") as staged:
                    staged.write_text(json.dumps(record), encoding="utf-8")
            else:
                revision = plan["head"]["revision"]
                with tempfile.TemporaryDirectory(prefix="dagoberd-snapshot-restore-") as directory:
                    copy = Path(directory) / "snapshot.aex"
                    shutil.copyfile(namespace / f"{revision}.aex", copy)
                    if file_hash(copy) != plan["head"]["sha256"]:
                        raise ValueError("Snapshot changed during restore; retry")
                    self.portability.restore(copy)
            self._save_state(revision)
        return self.status()


def adopt_snapshot(
    registry: WorkspaceRegistry,
    name: str,
    directory: str,
    snapshot: str,
    fingerprint: str,
    *,
    yes: bool,
) -> dict[str, Any]:
    if not yes:
        raise ValueError("Snapshot adoption requires local confirmation with --yes")
    inspected = inspect_snapshot(snapshot)
    if inspected["fingerprint"] != fingerprint:
        raise ValueError("Snapshot changed; inspect it again before adoption")
    target = Path(directory).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Snapshot adoption requires a new or empty local directory")
    if any(entry.id == inspected["workspace_id"] for entry in registry.read().workspaces):
        raise ValueError("Workspace identity is already registered")
    source = Path(inspected["snapshot"])
    if target.is_relative_to(source.parent.parent.parent):
        raise ValueError("Keep the active workspace outside the synchronized snapshot root")
    with tempfile.TemporaryDirectory(prefix="dagoberd-snapshot-adopt-") as temporary:
        bundle = Path(temporary) / "source.aex"
        with source.with_suffix(".aex").open("rb") as incoming, bundle.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if file_hash(bundle) != inspected["sha256"]:
            raise ValueError("Snapshot changed during adoption")
        target.mkdir(parents=True, exist_ok=True)
        document = tomlkit.parse(DEFAULT_CONFIG_TOML)
        document["workspace"] = {"id": inspected["workspace_id"], "name": name}
        atomic_text(target / "dagoberd.toml", tomlkit.dumps(document))
        entry = registry.initialize(name, target)
        service = WorkspaceSync(registry, entry["id"])
        service.configure(str(source.parent.parent.parent), yes=True)
        with workspace_operation(service.loaded, registry, exclusive=True):
            service.portability.restore(bundle)
            service._save_state(inspected["revision"])
    return {"workspace_id": entry["id"], "adopted": True, "files_preserved": True}

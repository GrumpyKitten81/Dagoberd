from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomlkit
from pydantic import Field

from dagoberd.config import (
    DEFAULT_CONFIG_TOML,
    LoadedConfig,
    StrictModel,
    load_config,
)
from dagoberd.workspace_lock import file_lock

CURRENT_WORKSPACE: ContextVar[LoadedConfig | None] = ContextVar("workspace", default=None)
REGISTRY_ENV = "ACCOUNT_EVAL_WORKSPACE_REGISTRY"
IGNORE_RULES = (
    ".env",
    ".env.*",
    "account-eval.local.toml",
    ".account-eval/",
    "*-wal",
    "*-shm",
    "*-journal",
    "*.lock",
    ".account-eval-write-*/",
)


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".account-eval-write-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


class WorkspaceEntry(StrictModel):
    id: str
    name: str
    display_name: str
    config_path: Path
    private_remote: str | None = None
    storage_paths: list[Path] = Field(default_factory=list)


class RegistryData(StrictModel):
    version: int = 1
    workspaces: list[WorkspaceEntry] = Field(default_factory=list)


def default_registry_path() -> Path:
    if override := os.environ.get(REGISTRY_ENV):
        return Path(override).expanduser().resolve()
    base = Path(
        os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    )
    return base / "account-eval" / "workspaces.toml"


def storage_paths(loaded: LoadedConfig) -> list[Path]:
    config = loaded.config
    paths = [
        config.database.path,
        config.documents.storage_dir,
        config.reporting.output_dir,
        loaded.project_path.parent / ".account-eval",
    ]
    if config.cloud.workspace is not None:
        paths.append(config.cloud.workspace)
    return [path.resolve() for path in paths]


def overlaps(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


class WorkspaceRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path else default_registry_path()

    def read(self) -> RegistryData:
        if not self.path.exists():
            return RegistryData()
        with self.path.open("rb") as handle:
            data = RegistryData.model_validate(tomllib.load(handle))
        if data.version != 1:
            raise ValueError("Unsupported workspace registry version")
        return data

    def _write(self, data: RegistryData) -> None:
        atomic_text(self.path, tomlkit.dumps(data.model_dump(mode="json", exclude_none=True)))

    def list(self) -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry in self.read().workspaces]

    def entry(self, selector: str) -> WorkspaceEntry:
        for entry in self.read().workspaces:
            if selector in (entry.id, entry.name):
                return entry
        raise ValueError("Unknown workspace")

    def validate(self, loaded: LoadedConfig) -> None:
        identity = str(loaded.config.workspace.id)
        paths = storage_paths(loaded)
        for entry in self.read().workspaces:
            if entry.id == identity:
                if entry.config_path.resolve() != loaded.project_path.resolve():
                    raise ValueError("Workspace identity is registered at a different path")
                continue
            other_paths = entry.storage_paths
            if entry.config_path.is_file():
                other = load_config(entry.config_path, use_environment=False)
                other_paths = storage_paths(other)
            if any(overlaps(left, right) for left in paths for right in other_paths):
                raise ValueError("Workspace storage overlaps another registered workspace")
        cloud = loaded.config.cloud
        if cloud.root is not None:
            expected = cloud.root.resolve() / "workspaces" / identity
            if expected.resolve() != expected:
                raise ValueError("Cloud workspace namespace cannot be redirected through links")
            for path in (loaded.config.documents.storage_dir, loaded.config.reporting.output_dir):
                if not path.resolve().is_relative_to(expected):
                    raise ValueError("Cloud file paths must stay inside this workspace namespace")

    def load(self, selector: str) -> LoadedConfig:
        entry = self.entry(selector)
        loaded = load_config(entry.config_path, use_environment=False)
        if str(loaded.config.workspace.id) != entry.id:
            raise ValueError("Workspace identity changed; repair the configuration before use")
        self.validate(loaded)
        return replace(loaded, registry_path=self.path)

    def validate_output(self, loaded: LoadedConfig, destination: str | Path) -> None:
        if loaded.config.workspace.id is None:
            return
        target = Path(destination).resolve()
        for entry in self.read().workspaces:
            if entry.id == str(loaded.config.workspace.id):
                continue
            other = self.load(entry.id)
            if any(
                overlaps(target, path)
                for path in [other.project_path.parent, *storage_paths(other)]
            ):
                raise ValueError("Output destination overlaps another workspace")

    def add(
        self, name: str, config_path: str | Path, *, display_name: str | None = None
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise ValueError("Workspace names must be lowercase slugs (up to 64 characters)")
        path = Path(config_path).resolve()
        with file_lock(self.path.with_suffix(".lock"), exclusive=True):
            data = self.read()
            if any(
                entry.name == name or entry.config_path.resolve() == path
                for entry in data.workspaces
            ):
                raise ValueError("Workspace name or configuration is already registered")
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            overrides = None
            if not document.get("workspace", {}).get("id"):
                overrides = {"workspace": {"id": str(uuid4()), "name": display_name or name}}
                document["workspace"] = overrides["workspace"]
            loaded = load_config(path, overrides=overrides, use_environment=False)
            identity = str(loaded.config.workspace.id)
            if any(entry.id == identity for entry in data.workspaces):
                raise ValueError("Workspace identity is already registered")
            self.validate(loaded)
            entry = WorkspaceEntry(
                id=identity,
                name=name,
                display_name=display_name or loaded.config.workspace.name or name,
                config_path=path,
                storage_paths=storage_paths(loaded),
            )
            if overrides is not None:
                atomic_text(path, tomlkit.dumps(document))
            data.workspaces.append(entry)
            self._write(data)
            return entry.model_dump(mode="json")

    def remove(self, selector: str) -> None:
        with file_lock(self.path.with_suffix(".lock"), exclusive=True):
            entry = self.entry(selector)
            data = self.read()
            data.workspaces = [item for item in data.workspaces if item.id != entry.id]
            self._write(data)

    def refresh_storage(self, selector: str) -> None:
        loaded = self.load(selector)
        with file_lock(self.path.with_suffix(".lock"), exclusive=True):
            data = self.read()
            for entry in data.workspaces:
                if entry.id == str(loaded.config.workspace.id):
                    entry.storage_paths = storage_paths(loaded)
            self._write(data)

    def initialize(
        self,
        name: str,
        directory: str | Path,
        *,
        create: bool = False,
        cloud_root: str | Path | None = None,
    ) -> dict[str, Any]:
        root = Path(directory).resolve()
        if create and root.exists() and any(root.iterdir()):
            raise ValueError("New workspace directory must be empty")
        root.mkdir(parents=True, exist_ok=True)
        path = root / "account-eval.toml"
        if not path.exists():
            document = tomlkit.parse(DEFAULT_CONFIG_TOML)
            document["workspace"] = {"id": str(uuid4()), "name": name}
            atomic_text(path, tomlkit.dumps(document))
        if cloud_root is not None:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            if not document.get("workspace", {}).get("id"):
                candidate = {"workspace": {"id": str(uuid4()), "name": name}}
                self.validate(load_config(path, overrides=candidate, use_environment=False))
                document["workspace"] = candidate["workspace"]
                atomic_text(path, tomlkit.dumps(document))
        if cloud_root is not None:
            desired_root = Path(cloud_root).resolve()
            configured = load_config(path, use_environment=False)
            if configured.config.cloud.root is not None:
                if configured.config.cloud.root.resolve() != desired_root:
                    raise ValueError(
                        "Use a cloud relocation preview to change existing cloud storage"
                    )
            elif configured.config.cloud.workspace is not None:
                raise ValueError("Use a cloud relocation preview to change existing cloud storage")
            else:
                local_path = path.with_name("account-eval.local.toml")
                local = (
                    tomlkit.parse(local_path.read_text(encoding="utf-8"))
                    if local_path.exists()
                    else tomlkit.document()
                )
                local["cloud"] = {"root": str(desired_root)}
                atomic_text(local_path, tomlkit.dumps(local))
        matching = next(
            (entry for entry in self.read().workspaces if entry.config_path.resolve() == path), None
        )
        if matching and matching.name != name:
            raise ValueError("Workspace is already registered under another name")
        result = matching.model_dump(mode="json") if matching else self.add(name, path)
        loaded = self.load(str(result["id"]))
        ignore_path = root / ".gitignore"
        ignored = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        additions = [rule for rule in IGNORE_RULES if rule not in ignored.splitlines()]
        if additions:
            atomic_text(ignore_path, ignored.rstrip() + "\n" + "\n".join(additions) + "\n")
        from dagoberd.ledger.database import LedgerDatabase
        from dagoberd.ledger.repository import LedgerRepository
        from dagoberd.profiles import install_builtin_profiles

        with workspace_operation(loaded, self, exclusive=True):
            install_builtin_profiles(LedgerRepository(LedgerDatabase(loaded.config.database.path)))
        return result


def resolve_workspace(
    selector: str | None = None,
    config_path: str | Path | None = None,
    db_path: str | None = None,
    *,
    registry: WorkspaceRegistry | None = None,
) -> LoadedConfig:
    registry = registry or WorkspaceRegistry()
    if selector and config_path:
        raise ValueError("Use either --workspace or --config")
    entries = registry.read().workspaces
    if selector:
        loaded = registry.load(selector)
    elif config_path is not None:
        matching = next(
            (
                entry
                for entry in entries
                if entry.config_path.resolve() == Path(config_path).resolve()
            ),
            None,
        )
        loaded = registry.load(matching.id) if matching else load_config(config_path)
    elif len(entries) > 1:
        raise ValueError("Select a workspace explicitly with --workspace or --config")
    elif entries:
        loaded = registry.load(entries[0].id)
    else:
        loaded = load_config()
    if loaded.config.workspace.id is not None:
        if db_path:
            raise ValueError("Database overrides are unavailable for workspaces")
        registry.validate(loaded)
    elif db_path:
        loaded = load_config(
            loaded.project_path if loaded.project_path.is_file() else None,
            overrides={"database": {"path": db_path}},
        )
    if loaded.config.workspace.id is None and entries:
        for entry in entries:
            registered = registry.load(entry.id)
            if registered.config.database.path.resolve() != loaded.config.database.path.resolve():
                continue
            if db_path:
                raise ValueError("Database overrides are unavailable for registered ledgers")
            candidate = loaded.config.model_copy(update={"workspace": registered.config.workspace})
            if candidate != registered.config:
                raise ValueError("This ledger is registered; select its workspace configuration")
            loaded = registered
            break
        registry.validate(loaded)
    return replace(loaded, registry_path=registry.path) if loaded.config.workspace.id else loaded


@contextmanager
def workspace_operation(
    loaded: LoadedConfig, registry: WorkspaceRegistry | None = None, *, exclusive: bool = False
) -> Iterator[None]:
    registry = registry or WorkspaceRegistry(loaded.registry_path)
    token = CURRENT_WORKSPACE.set(loaded)
    try:
        if loaded.config.workspace.id is None:
            yield
        else:
            with (
                file_lock(
                    loaded.project_path.parent / ".account-eval" / "workspace.lock",
                    exclusive=exclusive,
                ),
                file_lock(
                    loaded.config.database.path.with_suffix(".workspace.lock"), exclusive=exclusive
                ),
            ):
                if (loaded.project_path.parent / ".account-eval" / "cloud-migration.json").exists():
                    raise ValueError(
                        "Cloud relocation is incomplete; use workspaces cloud <name> recover --yes"
                    )
                current = load_config(loaded.project_path, use_environment=False)
                if current.config != loaded.config:
                    raise ValueError("Workspace configuration changed; reload before retrying")
                registry.validate(loaded)
                if loaded.config.database.path.is_file():
                    from dagoberd.ledger.database import LedgerDatabase

                    LedgerDatabase.validate_workspace_identity(loaded.config.database.path)
                yield
    finally:
        CURRENT_WORKSPACE.reset(token)

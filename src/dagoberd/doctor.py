"""Local, non-networking diagnostics for an Dagoberd project."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from dagoberd.config import AppConfig, LoadedConfig
from dagoberd.ledger.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS
from dagoberd.secrets import SecretStore


@dataclass(frozen=True, slots=True)
class Diagnostic:
    name: str
    status: Literal["ok", "warning", "error"]
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def run_diagnostics(
    loaded: LoadedConfig,
    secret_store: SecretStore | None = None,
) -> list[Diagnostic]:
    config = loaded.config
    secrets = secret_store or SecretStore(dotenv_path=loaded.project_path.with_name(".env"))
    diagnostics = [
        Diagnostic(
            "python",
            "ok" if sys.version_info >= (3, 12) else "error",
            (f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
        ),
        Diagnostic("configuration", "ok", str(loaded.project_path)),
    ]
    diagnostics.extend(_data_root_diagnostics(config))
    diagnostics.extend(_cloud_workspace_diagnostics(config))
    diagnostics.extend(_database_diagnostics(config.database.path, config.data.root))
    keyring_ok, keyring_message = secrets.keyring_status()
    diagnostics.append(
        Diagnostic(
            "keyring",
            "ok" if keyring_ok else "warning",
            keyring_message,
        )
    )
    diagnostics.extend(_comdirect_diagnostic(loaded, secrets))
    diagnostics.append(_llm_diagnostic(loaded, secrets))
    try:
        import mcp  # noqa: F401

        diagnostics.append(Diagnostic("mcp", "ok", "installed"))
    except ImportError:
        diagnostics.append(Diagnostic("mcp", "error", "package not installed"))
    return diagnostics


def _data_root_diagnostics(config: AppConfig) -> list[Diagnostic]:
    data_root = config.data.root
    if data_root is None:
        return [
            Diagnostic(
                "data.root",
                "warning",
                "not configured; database defaults to project output/",
            )
        ]
    if not data_root.is_dir():
        return [Diagnostic("data.root", "error", f"missing directory: {data_root}")]
    git_present = (data_root / ".git").exists()
    writable = os.access(data_root, os.W_OK)
    status: Literal["ok", "warning", "error"] = "ok" if writable else "error"
    message = str(data_root)
    if git_present:
        message += "; git repository detected"
    if not writable:
        message += "; not writable"
    return [Diagnostic("data.root", status, message)]


def _cloud_workspace_diagnostics(config: AppConfig) -> list[Diagnostic]:
    workspace = config.cloud.workspace
    if workspace is None:
        return [
            Diagnostic(
                "cloud.workspace",
                "warning",
                "not configured; documents and reports use project output/",
            )
        ]
    if not workspace.is_dir():
        return [Diagnostic("cloud.workspace", "error", f"missing directory: {workspace}")]
    missing = [
        name for name in ("documents", "reports", "imports") if not (workspace / name).is_dir()
    ]
    status: Literal["ok", "warning"] = "warning" if missing else "ok"
    message = str(workspace)
    if missing:
        message += f"; missing subdirectories: {', '.join(missing)}"
    return [Diagnostic("cloud.workspace", status, message)]


def _database_diagnostics(path: Path, data_root: Path | None) -> list[Diagnostic]:
    if not path.is_file():
        return [
            Diagnostic(
                "database",
                "warning",
                f"not initialized: {path}",
            )
        ]
    try:
        with sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
        ) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='schema_migrations'"""
            ).fetchone()
            if table is None:
                return [
                    Diagnostic(
                        "database",
                        "warning",
                        f"schema migration required: {path}",
                    )
                ]
            version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            applied = {
                row[0]: row[1]
                for row in connection.execute("SELECT version, checksum FROM schema_migrations")
            }
    except (sqlite3.DatabaseError, RuntimeError, ValueError) as error:
        return [Diagnostic("database", "error", str(error))]
    expected = {migration.version: migration.checksum for migration in MIGRATIONS}
    checksum_valid = all(applied.get(version) == checksum for version, checksum in expected.items())
    diagnostics = [
        Diagnostic(
            "database",
            "ok" if integrity == "ok" else "error",
            str(path),
        ),
        Diagnostic(
            "schema",
            ("ok" if version == CURRENT_SCHEMA_VERSION and checksum_valid else "error"),
            (
                f"version {version}/{CURRENT_SCHEMA_VERSION}; "
                f"checksums {'ok' if checksum_valid else 'invalid'}"
            ),
        ),
    ]
    if data_root is not None:
        try:
            outside_data_root = not path.resolve().is_relative_to(data_root.resolve())
        except ValueError:
            outside_data_root = True
        if outside_data_root:
            diagnostics.append(
                Diagnostic(
                    "database.location",
                    "warning",
                    f"database is outside data.root ({data_root})",
                )
            )
    return diagnostics


def _comdirect_diagnostic(
    loaded: LoadedConfig,
    secrets: SecretStore,
) -> list[Diagnostic]:
    connections = loaded.config.comdirect.connections
    if not connections:
        return [Diagnostic("comdirect", "warning", "no named connections configured")]
    diagnostics: list[Diagnostic] = []
    for name, config in sorted(connections.items()):
        component = f"comdirect.{name}"
        if not config.enabled:
            diagnostics.append(Diagnostic(component, "warning", "disabled"))
            continue
        secret_names = (
            config.username_secret,
            config.client_id_secret,
            config.client_secret_secret,
        )
        missing = [secret for secret in secret_names if not secrets.get_keyring(secret)]
        if missing:
            diagnostics.append(
                Diagnostic(
                    component,
                    "error",
                    "missing secret references: " + ", ".join(missing),
                )
            )
            continue
        diagnostics.append(Diagnostic(component, "ok", "configured (not connected)"))
    return diagnostics


def _llm_diagnostic(
    loaded: LoadedConfig,
    secrets: SecretStore,
) -> Diagnostic:
    config = loaded.config.llm
    if not config.enabled:
        return Diagnostic("llm", "warning", "disabled")
    if config.mode == "cloud" and not secrets.get(config.api_key_secret):
        return Diagnostic(
            "llm",
            "error",
            f"missing secret reference: {config.api_key_secret}",
        )
    return Diagnostic(
        "llm",
        "ok",
        f"{config.mode}/{config.provider}/{config.model}",
    )

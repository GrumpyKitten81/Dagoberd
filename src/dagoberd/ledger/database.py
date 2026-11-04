"""SQLite bootstrap, migrations, backup, and integrity checks."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from dagoberd.safe_files import prepared_new_file

from .migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    apply_pending_migrations,
)


class LedgerDatabase:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or "output/account_eval.db")

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        new_workspace = not self.path.is_file() or self.path.stat().st_size == 0
        if not new_workspace:
            self.validate_workspace_identity(self.path)
        if self._schema_is_current():
            self._bind_workspace()
            return
        if self._needs_migration_backup():
            self.backup()
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with closing(self.connect()) as connection, connection:
            ledger_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_transactions'"
            ).fetchone()
            if ledger_exists:
                apply_pending_migrations(connection)
                connection.executescript(schema)
            else:
                connection.executescript(schema)
                apply_pending_migrations(connection)
            if new_workspace:
                connection.executemany(
                    "UPDATE ledger_dimensions SET name=? WHERE id=?",
                    [
                        (name, f"builtin-dimension-{code}")
                        for code, name in (
                            ("person", "Person"),
                            ("household", "Household"),
                            ("property", "Property"),
                            ("project", "Project"),
                        )
                    ],
                )

        self._bind_workspace()

    def _bind_workspace(self) -> None:
        from dagoberd.workspaces import CURRENT_WORKSPACE

        loaded = CURRENT_WORKSPACE.get()
        if loaded is None or loaded.config.workspace.id is None:
            return
        if loaded.config.database.path.resolve() != self.path.resolve():
            raise ValueError("Database path differs from the selected workspace")
        identity = str(loaded.config.workspace.id)
        with closing(self.connect()) as connection, connection:
            existing = connection.execute(
                "SELECT workspace_id FROM ledger_workspace_identity WHERE singleton=1"
            ).fetchone()
            if existing and existing[0] != identity:
                raise ValueError("Ledger belongs to another workspace")
            if existing is None:
                for table, previous, invalidated in (
                    ("ledger_import_previews", "prepared", "failed"),
                    ("ledger_bulk_previews", "prepared", "expired"),
                    ("ledger_llm_previews", "ready", "invalidated"),
                    ("ledger_llm_previews", "running", "invalidated"),
                    ("ledger_document_batch_previews", "prepared", "failed"),
                    ("ledger_delivery_previews", "ready", "invalidated"),
                ):
                    connection.execute(
                        f"UPDATE {table} SET status=? WHERE status=?", (invalidated, previous)
                    )
            connection.execute(
                "INSERT OR IGNORE INTO ledger_workspace_identity(singleton, workspace_id) VALUES (1, ?)",
                (identity,),
            )

    @staticmethod
    def validate_workspace_identity(path: Path) -> None:
        from dagoberd.workspaces import CURRENT_WORKSPACE

        loaded = CURRENT_WORKSPACE.get()
        if loaded is None or loaded.config.workspace.id is None:
            return
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_workspace_identity'"
            ).fetchone()
            row = (
                connection.execute(
                    "SELECT workspace_id FROM ledger_workspace_identity WHERE singleton=1"
                ).fetchone()
                if table
                else None
            )
            if row and row[0] != str(loaded.config.workspace.id):
                raise ValueError("Ledger belongs to another workspace")

    def _schema_is_current(self) -> bool:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return False
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('ledger_transactions', 'schema_migrations')"
                )
            }
            if tables != {"ledger_transactions", "schema_migrations"}:
                return False
            applied = {
                int(row[0]): (row[1], row[2])
                for row in connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations"
                )
            }
            expected = {migration.version: migration for migration in MIGRATIONS}
            return set(applied) == set(expected) and all(
                applied[version] == (migration.name, migration.checksum)
                for version, migration in expected.items()
            )
        finally:
            connection.close()

    def _needs_migration_backup(self) -> bool:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return False
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if table is None:
                return True
            applied = {
                int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
            }
            return any(migration.version not in applied for migration in MIGRATIONS)
        finally:
            connection.close()

    def backup(self, destination: str | Path | None = None) -> Path:
        if not self.path.is_file():
            raise FileNotFoundError(f"Database does not exist: {self.path}")
        if destination is None:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            destination_path = (
                self.path.parent
                / "backups"
                / (
                    f"{self.path.stem}.before-migration-v{CURRENT_SCHEMA_VERSION}.{timestamp}.sqlite3"
                )
            )
        else:
            destination_path = Path(destination)
        if destination_path.resolve() == self.path.resolve():
            raise ValueError("Backup destination must differ from the database")
        with (
            prepared_new_file(destination_path) as staged,
            closing(self._read_only_connection()) as source,
            closing(sqlite3.connect(staged)) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Created database backup failed integrity check")
        return destination_path

    def restore(self, backup_path: str | Path) -> None:
        source_path = Path(backup_path)
        if not source_path.is_file():
            raise FileNotFoundError(f"Backup does not exist: {source_path}")
        if source_path.resolve() == self.path.resolve():
            raise ValueError("Backup source must differ from the database")
        self.validate_workspace_identity(source_path)
        with closing(sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed SQLite integrity check")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Restored database failed integrity check")

    def export_json(self, destination: str | Path) -> Path:
        if not self.path.is_file():
            raise FileNotFoundError(f"Database does not exist: {self.path}")
        target = Path(destination).absolute()
        with closing(self._read_only_connection()) as connection:
            connection.execute("BEGIN")
            tables = [
                row["name"]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master WHERE type='table'
                       AND (name LIKE 'ledger_%' OR name='schema_migrations') ORDER BY name"""
                )
            ]
            payload = {
                "format": "account-eval-ledger-export-v1",
                "created_at": datetime.now(UTC).isoformat(),
                "tables": {
                    table: [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                    for table in tables
                },
            }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        with prepared_new_file(target) as staged:
            staged.write_text(serialized, encoding="utf-8")
            if json.loads(staged.read_text(encoding="utf-8")) != json.loads(serialized):
                raise ValueError("Ledger export failed JSON verification")
        return target

    def _read_only_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def integrity_check(self) -> dict:
        if not self.path.is_file():
            return {"valid": False, "database": str(self.path), "error": "missing"}
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = [
                dict(zip(("table", "rowid", "parent", "fkid"), row, strict=True))
                for row in connection.execute("PRAGMA foreign_key_check")
            ]
            migrations = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        return {
            "valid": integrity == ["ok"] and not foreign_keys,
            "database": str(self.path),
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
            "schema_version": max(migrations, default=0),
        }

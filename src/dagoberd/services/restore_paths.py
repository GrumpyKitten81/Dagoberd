from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from dagoberd.ledger.database import LedgerDatabase


def _source_root(value: object) -> PurePath:
    if not isinstance(value, str) or not value:
        raise ValueError("Restore references require an absolute source root")
    windows = PureWindowsPath(value)
    root = windows if windows.is_absolute() else PurePosixPath(value)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Restore references require a safe absolute source root")
    return root


def _reference_destination(
    value: object,
    source_root: object,
    scope: str,
    destinations: dict[PurePath, list[Path]],
) -> str:
    root = _source_root(source_root)
    if not isinstance(value, str) or not value:
        raise ValueError("Restore file reference must be an absolute path")
    path = type(root)(value)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("Restore file reference is outside its exported source root")
    relative = path.relative_to(root)
    matches = destinations.get(type(root)(scope, *relative.parts), [])
    if not matches:
        raise ValueError("Restore file reference has no matching archived file")
    if len(matches) != 1:
        raise ValueError("Restore file reference is ambiguous")
    return str(matches[0])


def rebase_archive_references(
    database: LedgerDatabase,
    source_roots: object,
    files: list[tuple[str, Path]],
) -> None:
    if not isinstance(source_roots, dict):
        raise ValueError("Restore source roots must be an object")
    destinations: dict[PurePath, list[Path]] = {}
    for name, destination in files:
        parts = PurePosixPath(name).parts
        for key in (PurePosixPath(*parts), PureWindowsPath(*parts)):
            destinations.setdefault(key, []).append(destination)
    with closing(database.connect()) as connection, connection:
        documents = connection.execute("SELECT id, storage_path FROM ledger_documents").fetchall()
        for document in documents:
            if not document["storage_path"]:
                continue
            rebased_document = _reference_destination(
                document["storage_path"], source_roots.get("documents"), "documents", destinations
            )
            connection.execute(
                "UPDATE ledger_documents SET storage_path=? WHERE id=?",
                (rebased_document, document["id"]),
            )
        for table in ("ledger_report_runs", "ledger_analysis_runs"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            rows = connection.execute(f"SELECT id, output_paths_json FROM {table}").fetchall()
            for row in rows:
                paths = json.loads(row["output_paths_json"])
                if not isinstance(paths, list):
                    raise ValueError("Restore report references must be a list of absolute paths")
                rebased = [
                    _reference_destination(
                        path, source_roots.get("output"), "reports", destinations
                    )
                    for path in paths
                ]
                connection.execute(
                    f"UPDATE {table} SET output_paths_json=? WHERE id=?",
                    (json.dumps(rebased), row["id"]),
                )

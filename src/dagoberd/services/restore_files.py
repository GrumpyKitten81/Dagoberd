from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path


class RestoreError(RuntimeError):
    def __init__(
        self,
        *,
        rollback_complete: bool,
        rollback_export: Path | None,
        recovery_directory: Path | None,
        failures: list[dict[str, str]],
        pending_restore: bool = False,
        restore_complete: bool = False,
        cleanup_failures: list[dict[str, str]] | None = None,
    ) -> None:
        self.rollback_complete = rollback_complete
        self.restore_complete = restore_complete
        self.details = {
            "rollback_complete": rollback_complete,
            "rollback_export": str(rollback_export) if rollback_export else None,
            "recovery_directory": str(recovery_directory) if recovery_directory else None,
            "rollback_failures": failures,
            "restore_complete": restore_complete,
            "cleanup_failures": cleanup_failures or [],
        }
        self.code = "restore_recovery_required"
        if pending_restore:
            status = "Unresolved restore recovery; manual recovery required"
        elif restore_complete:
            self.code = "restore_cleanup_required"
            status = "Restore completed; temporary-file cleanup failed; manual cleanup required"
        elif rollback_complete:
            self.code = "restore_rolled_back"
            status = "Restore failed; previous state restored"
        else:
            status = "Restore and rollback failed; manual recovery required"
        if rollback_complete and cleanup_failures:
            status += "; temporary-file cleanup required"
        super().__init__(
            f"{status}. Rollback export: {rollback_export}. Recovery directory: {recovery_directory}"
        )


def validate_restore_targets(
    destinations: list[tuple[str, Path]],
    current_files: set[Path],
    *,
    documents_dir: Path,
    output_dir: Path,
    protected_paths: set[Path],
    protected_directories: tuple[Path, ...],
) -> None:
    for root in (documents_dir, output_dir):
        if root.exists() and not root.is_dir():
            raise ValueError(f"Restore root is not a directory: {root}")
    incoming = [path for _, path in destinations]
    if len(incoming) != len(set(incoming)):
        raise ValueError("Restore entries resolve to the same destination")
    all_targets = current_files | set(incoming)
    for path in all_targets:
        if any(
            path == protected or protected.is_relative_to(path) for protected in protected_paths
        ) or any(
            path.is_relative_to(root) or root.is_relative_to(path) for root in protected_directories
        ):
            raise ValueError(f"Restore conflicts with a protected path: {path}")
        if path in {documents_dir, output_dir} or (path.exists() and not path.is_file()):
            raise ValueError(f"Restore destination is not a regular file: {path}")
        root = documents_dir if path.is_relative_to(documents_dir) else output_dir
        if not path.is_relative_to(root):
            raise ValueError(f"Restore destination is outside managed roots: {path}")
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError(f"Restore does not support symbolic links: {component}")
            if component == root:
                break
            if component != path and component.exists() and not component.is_dir():
                raise ValueError(f"Restore parent is not a directory: {component}")
        if any(parent in all_targets for parent in path.parents):
            raise ValueError(f"Restore contains conflicting file and directory targets: {path}")
    for name, path in destinations:
        if name.startswith("reports/") and path.is_relative_to(documents_dir):
            raise ValueError("Report restore destination overlaps document storage")
        if path.exists() and path not in current_files:
            raise ValueError(f"Restore would overwrite an unmanaged file: {path}")


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def cleanup_restore_directory(path: Path, failures: list[dict[str, str]]) -> None:
    try:
        shutil.rmtree(path)
    except OSError as error:
        failures.append({"path": str(path), "error": type(error).__name__})


@dataclass
class RestoreFiles:
    incoming: dict[Path, Path] = field(default_factory=dict)
    incoming_hashes: dict[Path, str] = field(default_factory=dict)
    previous: dict[Path, Path] = field(default_factory=dict)
    touched: list[Path] = field(default_factory=list)

    @classmethod
    def prepare(
        cls,
        archive: zipfile.ZipFile,
        destinations: list[tuple[str, Path]],
        current_files: set[Path],
        stack: ExitStack,
        cleanup_failures: list[dict[str, str]],
    ) -> RestoreFiles:
        prepared = cls()
        staging: dict[Path, Path] = {}
        for index, destination in enumerate(sorted(current_files | {p for _, p in destinations})):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent not in staging:
                directory = Path(
                    tempfile.mkdtemp(prefix=".account-eval-restore-", dir=destination.parent)
                )
                stack.callback(cleanup_restore_directory, directory, cleanup_failures)
                staging[destination.parent] = directory
            if destination in current_files:
                previous = staging[destination.parent] / f"previous-{index}"
                shutil.copy2(destination, previous)
                if file_hash(previous) != file_hash(destination):
                    raise ValueError(f"Restore target changed during preparation: {destination}")
                prepared.previous[destination] = previous
        for index, (name, destination) in enumerate(destinations):
            staged = staging[destination.parent] / f"incoming-{index}"
            payload = archive.read(name)
            expected_hash = hashlib.sha256(payload).hexdigest()
            staged.write_bytes(payload)
            os.chmod(staged, 0o600)
            if file_hash(staged) != expected_hash:
                raise ValueError(f"Prepared restore file failed checksum validation: {destination}")
            prepared.incoming[destination] = staged
            prepared.incoming_hashes[destination] = expected_hash
        return prepared

    def recovery_entries(self) -> list[dict[str, str | None]]:
        return [
            {
                "destination": str(path),
                "incoming": str(self.incoming[path]) if path in self.incoming else None,
                "incoming_sha256": self.incoming_hashes.get(path),
                "previous": str(self.previous[path]) if path in self.previous else None,
                "previous_sha256": file_hash(self.previous[path])
                if path in self.previous
                else None,
            }
            for path in sorted(self.incoming.keys() | self.previous.keys())
        ]

    def apply(self) -> None:
        for destination, staged in self.incoming.items():
            self.touched.append(destination)
            os.replace(staged, destination)
        for destination in self.previous.keys() - self.incoming.keys():
            self.touched.append(destination)
            destination.unlink()

    def rollback(self) -> list[dict[str, str]]:
        failures = []
        for destination in reversed(self.touched):
            try:
                if destination in self.previous:
                    previous = self.previous[destination]
                    expected_hash = file_hash(previous)
                    os.replace(previous, destination)
                    if file_hash(destination) != expected_hash:
                        raise ValueError("Restored file differs from its previous content")
                else:
                    destination.unlink(missing_ok=True)
            except (OSError, ValueError, KeyboardInterrupt) as error:
                failures.append({"path": str(destination), "error": type(error).__name__})
        return failures

    def verify(self) -> None:
        for destination, expected_hash in self.incoming_hashes.items():
            if file_hash(destination) != expected_hash:
                raise ValueError(f"Restored file failed checksum validation: {destination}")

"""Versioned local export, encrypted backup, restore and safe deletion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from account_eval import __version__
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.migrations import CURRENT_SCHEMA_VERSION
from account_eval.safe_files import prepared_new_file
from account_eval.services.restore_files import (
    RestoreError,
    RestoreFiles,
    cleanup_restore_directory,
    validate_restore_targets,
)
from account_eval.services.restore_paths import rebase_archive_references

EXPORT_FORMAT = "account-eval-portable-export-v2"
ENCRYPTED_MAGIC = b"AEBAK2\0"


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    preview_id: str
    scopes: tuple[str, ...]
    targets: tuple[dict[str, Any], ...]
    total_bytes: int
    fingerprint: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PortabilityService:
    def __init__(
        self,
        database_path: str | Path,
        documents_dir: str | Path,
        output_dir: str | Path,
        project_path: str | Path,
        *,
        masked_config: dict | None = None,
    ) -> None:
        self.database = LedgerDatabase(database_path)
        self.documents_dir = Path(documents_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.project_path = Path(project_path).resolve()
        self.masked_config = masked_config or {}
        self.metadata_dir = self.project_path.parent / ".account-eval"

    def export(self, destination: str | Path) -> dict:
        target = Path(destination).absolute()
        with tempfile.TemporaryDirectory(prefix="account-eval-export-") as temporary:
            temporary_path = Path(temporary)
            ledger_copy = temporary_path / "ledger.sqlite3"
            self.database.backup(ledger_copy)
            members: list[tuple[str, Path, str]] = [
                ("ledger/ledger.sqlite3", ledger_copy, "ledger"),
                *self._managed_members(),
            ]
            config_path = temporary_path / "config.json"
            config_path.write_text(
                json.dumps(self.masked_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            members.append(("metadata/config.json", config_path, "configuration"))
            entries = [
                {
                    "path": archive_path,
                    "kind": kind,
                    "size": source.stat().st_size,
                    "sha256": self._hash(source),
                }
                for archive_path, source, kind in sorted(members)
            ]
            manifest = {
                "format": EXPORT_FORMAT,
                "product_version": __version__,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "source_roots": {
                    "output": str(self.output_dir),
                    "documents": str(self.documents_dir),
                },
                "entries": entries,
            }
            with prepared_new_file(target) as staged:
                with zipfile.ZipFile(
                    staged,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as archive:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                    )
                    for archive_path, source, _ in sorted(members):
                        archive.write(source, archive_path)
                verification = self.verify(staged)
        return {"path": str(target), **verification}

    def encrypted_backup(self, destination: str | Path, password: str) -> dict:
        if len(password) < 12:
            raise ValueError("Backup password must contain at least 12 characters")
        target = Path(destination).absolute()
        with tempfile.TemporaryDirectory(prefix="account-eval-backup-") as temporary:
            bundle = Path(temporary) / "portable.aex"
            exported = self.export(bundle)
            plaintext = bundle.read_bytes()
            salt, nonce = os.urandom(16), os.urandom(12)
            key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))
            header = {
                "format": "account-eval-encrypted-backup-v2",
                "kdf": "scrypt-n16384-r8-p1",
                "cipher": "AES-256-GCM",
                "salt": salt.hex(),
                "nonce": nonce.hex(),
                "bundle_sha256": exported["bundle_sha256"],
            }
            header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, header_bytes)
            with prepared_new_file(target) as staged:
                with staged.open("wb") as handle:
                    handle.write(ENCRYPTED_MAGIC)
                    handle.write(struct.pack(">I", len(header_bytes)))
                    handle.write(header_bytes)
                    handle.write(ciphertext)
                verification = self.verify(staged, password=password)
        return {"path": str(target), "encrypted": True, **verification}

    def verify(self, path: str | Path, *, password: str | None = None) -> dict:
        source = Path(path).resolve()
        with tempfile.TemporaryDirectory(prefix="account-eval-verify-") as temporary:
            bundle = self._decrypt_if_needed(source, Path(temporary), password)
            with zipfile.ZipFile(bundle) as archive:
                manifest = self._validated_manifest(archive)
                for entry in manifest["entries"]:
                    payload = archive.read(entry["path"])
                    if (
                        len(payload) != entry["size"]
                        or hashlib.sha256(payload).hexdigest() != entry["sha256"]
                    ):
                        raise ValueError(f"Checksum mismatch: {entry['path']}")
                database_copy = Path(temporary) / "verify.sqlite3"
                database_copy.write_bytes(archive.read("ledger/ledger.sqlite3"))
                with closing(
                    sqlite3.connect(f"file:{database_copy}?mode=ro", uri=True)
                ) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        raise ValueError("Export ledger failed SQLite integrity check")
            return {
                "valid": True,
                "format": manifest["format"],
                "schema_version": manifest["schema_version"],
                "entries": len(manifest["entries"]),
                "bundle_sha256": self._hash(source),
            }

    def restore(self, source: str | Path, *, password: str | None = None) -> dict:
        pending = sorted((self.metadata_dir / "restore-recovery").glob("restore-*"))
        if pending:
            raise RestoreError(
                rollback_complete=False,
                rollback_export=None,
                recovery_directory=pending[0],
                failures=[],
                pending_restore=True,
            )
        source_path = Path(source).resolve()
        with tempfile.TemporaryDirectory(prefix="account-eval-restore-") as temporary:
            temp_path = Path(temporary)
            snapshot = temp_path / "source"
            shutil.copyfile(source_path, snapshot)
            verification = self.verify(snapshot, password=password)
            bundle = self._decrypt_if_needed(snapshot, temp_path, password)
            with zipfile.ZipFile(bundle) as archive:
                manifest = self._validated_manifest(archive)
                ledger_staged = temp_path / "ledger.sqlite3"
                ledger_staged.write_bytes(archive.read("ledger/ledger.sqlite3"))
                LedgerDatabase.validate_workspace_identity(ledger_staged)
                staged_database = LedgerDatabase(ledger_staged)
                destinations = []
                for entry in manifest["entries"]:
                    destination = self._restore_destination(entry["path"])
                    if destination is not None:
                        destinations.append((entry["path"], destination))
                current_files = set(self._files(self.documents_dir)) | set(self._report_paths())
                database_path = self.database.path.resolve()
                validate_restore_targets(
                    destinations,
                    current_files,
                    documents_dir=self.documents_dir,
                    output_dir=self.output_dir,
                    protected_paths={
                        source_path,
                        database_path,
                        *(
                            Path(f"{database_path}{suffix}")
                            for suffix in ("-wal", "-shm", "-journal")
                        ),
                        self.project_path,
                        self.project_path.with_name("account-eval.local.toml"),
                        self.project_path.with_name(".env"),
                    },
                    protected_directories=(
                        self.metadata_dir.resolve(),
                        self.output_dir / "backups",
                    ),
                )
                rebase_archive_references(
                    staged_database, manifest.get("source_roots", {}), destinations
                )
                if not staged_database.integrity_check()["valid"]:
                    raise ValueError("Prepared restore database failed integrity checks")
                rollback = self._apply_prepared_restore(
                    archive, ledger_staged, destinations, current_files
                )
        return {
            "restored": True,
            "source": str(source_path),
            "rollback_export": str(rollback) if rollback else None,
            "restored_files": len(destinations),
            **verification,
        }

    def _apply_prepared_restore(
        self,
        archive: zipfile.ZipFile,
        ledger_staged: Path,
        destinations: list[tuple[str, Path]],
        current_files: set[Path],
    ) -> Path | None:
        rollback = None
        if self.database.path.is_file():
            rollback_dir = self.output_dir / "backups"
            rollback_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            rollback = rollback_dir / f"before-restore-{timestamp}-{uuid4().hex[:8]}.aex"
            self.export(rollback)
        recovery_root = self.metadata_dir / "restore-recovery"
        recovery_root.mkdir(parents=True, exist_ok=True)
        cleanup_failures: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        restore_error: BaseException | None = None
        with ExitStack() as stack:
            recovery = Path(tempfile.mkdtemp(prefix="restore-", dir=recovery_root))

            def cleanup_recovery() -> None:
                if not cleanup_failures:
                    cleanup_restore_directory(recovery, cleanup_failures)

            stack.callback(cleanup_recovery)
            previous_database = recovery / "previous.sqlite3"
            if rollback is not None:
                self.database.backup(previous_database)
                os.chmod(previous_database, 0o600)
            files = RestoreFiles.prepare(
                archive, destinations, current_files, stack, cleanup_failures
            )
            record = recovery / "recovery.json"
            record.write_text(
                json.dumps(
                    {
                        "database": str(self.database.path.resolve()),
                        "previous_database": str(previous_database) if rollback else None,
                        "rollback_export": str(rollback) if rollback else None,
                        "files": files.recovery_entries(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.chmod(record, 0o600)
            try:
                self.database.restore(ledger_staged)
                files.apply()
                if not self.database.integrity_check()["valid"]:
                    raise RuntimeError("Restored database failed post-restore integrity check")
                files.verify()
            except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyboardInterrupt) as error:
                restore_error = error
                failures = files.rollback()
                try:
                    if rollback is not None:
                        self.database.restore(previous_database)
                    else:
                        for suffix in ("-wal", "-shm", "-journal", ""):
                            Path(f"{self.database.path}{suffix}").unlink(missing_ok=True)
                except (
                    OSError,
                    sqlite3.Error,
                    ValueError,
                    RuntimeError,
                    KeyboardInterrupt,
                ) as rollback_error:
                    failures.append(
                        {"path": str(self.database.path), "error": type(rollback_error).__name__}
                    )
                if failures:
                    stack.pop_all()
        if restore_error is not None or cleanup_failures:
            raise RestoreError(
                rollback_complete=restore_error is not None and not failures,
                rollback_export=rollback,
                recovery_directory=recovery if failures or cleanup_failures else None,
                failures=failures,
                restore_complete=restore_error is None,
                cleanup_failures=cleanup_failures,
            ) from restore_error
        return rollback

    def integrity(self) -> dict:
        result = self.database.integrity_check()
        document_files = self._integrity_for_directory(self.documents_dir)
        missing_documents: list[str] = []
        changed_documents: list[str] = []
        missing_reports: list[str] = []
        split_violations: list[dict] = []
        if self.database.path.is_file():
            with closing(self.database.connect()) as connection:
                for row in connection.execute(
                    "SELECT id, storage_path, content_hash FROM ledger_documents"
                ):
                    if not row["storage_path"]:
                        continue
                    path = Path(row["storage_path"])
                    if not path.is_file():
                        missing_documents.append(row["id"])
                    elif row["content_hash"] and self._hash(path) != row["content_hash"]:
                        changed_documents.append(row["id"])
                for table in ("ledger_report_runs", "ledger_analysis_runs"):
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    if not exists:
                        continue
                    for row in connection.execute(f"SELECT id, output_paths_json FROM {table}"):
                        for value in json.loads(row["output_paths_json"] or "[]"):
                            if not Path(value).is_file():
                                missing_reports.append(f"{table}:{row['id']}:{Path(value).name}")
                split_violations = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT t.id AS transaction_id, t.state,
                              t.amount_minor AS parent_amount_minor,
                              COALESCE(SUM(s.amount_minor), 0) AS split_amount_minor,
                              COUNT(s.id) AS split_count
                       FROM ledger_transactions t
                       LEFT JOIN ledger_splits s ON s.transaction_id=t.id
                       GROUP BY t.id
                       HAVING (t.state='split_parent' AND
                                  (COUNT(s.id)=0 OR SUM(s.amount_minor)<>t.amount_minor))
                           OR (t.state<>'split_parent' AND COUNT(s.id)>0)"""
                    )
                ]
        result["documents"] = {
            **document_files,
            "missing": missing_documents,
            "checksum_mismatches": changed_documents,
        }
        result["reports"] = {
            "registered": len(self._report_paths()),
            "missing": missing_reports,
        }
        result["split_violations"] = split_violations
        result["valid"] = bool(
            result["valid"]
            and not (missing_documents or changed_documents or missing_reports or split_violations)
        )
        return result

    def preview_deletion(self, scopes: list[str]) -> DeletionPreview:
        allowed = {"database", "documents", "reports", "backups", "exports"}
        normalized = tuple(sorted(set(scopes)))
        unknown = set(normalized) - allowed
        if not normalized or unknown:
            raise ValueError(
                "Deletion scopes must be one or more of: " + ", ".join(sorted(allowed))
            )
        paths: set[Path] = set()
        if "database" in normalized:
            paths.update(
                path
                for path in (
                    self.database.path,
                    Path(f"{self.database.path}-wal"),
                    Path(f"{self.database.path}-shm"),
                )
                if path.is_file()
            )
        if "documents" in normalized:
            paths.update(self._files(self.documents_dir))
        if "reports" in normalized:
            paths.update(self._report_paths())
        if "backups" in normalized:
            paths.update(self._files(self.output_dir / "backups"))
        if "exports" in normalized:
            paths.update(
                path
                for path in self._files(self.output_dir)
                if path.suffix.casefold() in {".aex", ".aebak"}
            )
        targets: tuple[dict[str, Any], ...] = tuple(
            {
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": self._hash(path),
            }
            for path in sorted(paths)
        )
        created_at = datetime.now(UTC).isoformat()
        fingerprint = hashlib.sha256(
            json.dumps(targets, sort_keys=True).encode("utf-8")
        ).hexdigest()
        preview = DeletionPreview(
            uuid4().hex,
            normalized,
            targets,
            sum(item["size"] for item in targets),
            fingerprint,
            created_at,
        )
        directory = self.metadata_dir / "deletion-previews"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{preview.preview_id}.json"
        path.write_text(json.dumps(preview.as_dict(), indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
        return preview

    def execute_deletion(self, preview_id: str, fingerprint: str) -> dict:
        preview_path = self.metadata_dir / "deletion-previews" / f"{preview_id}.json"
        if not preview_path.is_file():
            raise KeyError(f"Unknown deletion preview {preview_id}")
        payload = json.loads(preview_path.read_text(encoding="utf-8"))
        if payload["fingerprint"] != fingerprint:
            raise ValueError("Deletion fingerprint does not match preview")
        for target in payload["targets"]:
            path = Path(target["path"])
            if not path.is_file() or self._hash(path) != target["sha256"]:
                raise ValueError(f"Deletion target changed after preview: {path}")
        deleted = []
        for target in payload["targets"]:
            path = Path(target["path"])
            path.unlink()
            deleted.append(str(path))
        receipt_dir = self.metadata_dir / "deletion-receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipt_dir / f"{preview_id}.json"
        receipt.write_text(
            json.dumps(
                {
                    **payload,
                    "deleted_at": datetime.now(UTC).isoformat(),
                    "deleted": deleted,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(receipt, 0o600)
        preview_path.unlink()
        return {"deleted": len(deleted), "receipt": str(receipt)}

    def _managed_members(self) -> list[tuple[str, Path, str]]:
        members = [
            (f"documents/{path.relative_to(self.documents_dir).as_posix()}", path, "document")
            for path in self._files(self.documents_dir)
        ]
        for path in self._report_paths():
            relative = path.relative_to(self.output_dir).as_posix()
            members.append((f"reports/{relative}", path, "report"))
        return members

    def _report_paths(self) -> list[Path]:
        if not self.database.path.is_file():
            return []
        found: set[Path] = set()
        with closing(self.database.connect()) as connection:
            for table in ("ledger_report_runs", "ledger_analysis_runs"):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                for row in connection.execute(f"SELECT output_paths_json FROM {table}"):
                    for value in json.loads(row[0] or "[]"):
                        path = Path(value).resolve()
                        if path.is_file() and self._within(path, self.output_dir):
                            found.add(path)
        return sorted(found)

    def _restore_destination(self, archive_path: str) -> Path | None:
        if archive_path.startswith("documents/"):
            return self._safe_join(self.documents_dir, archive_path.removeprefix("documents/"))
        if archive_path.startswith("reports/"):
            return self._safe_join(
                self.output_dir,
                archive_path.removeprefix("reports/"),
            )
        return None

    def _decrypt_if_needed(self, source: Path, temporary: Path, password: str | None) -> Path:
        with source.open("rb") as handle:
            magic = handle.read(len(ENCRYPTED_MAGIC))
            if magic != ENCRYPTED_MAGIC:
                return source
            if not password:
                raise ValueError("Encrypted backup requires a password")
            raw_size = handle.read(4)
            if len(raw_size) != 4:
                raise ValueError("Invalid encrypted backup header")
            header_size = struct.unpack(">I", raw_size)[0]
            if header_size > 64 * 1024:
                raise ValueError("Invalid encrypted backup header size")
            header_bytes = handle.read(header_size)
            header = json.loads(header_bytes)
            ciphertext = handle.read()
        key = Scrypt(
            salt=bytes.fromhex(header["salt"]),
            length=32,
            n=2**14,
            r=8,
            p=1,
        ).derive(password.encode("utf-8"))
        try:
            plaintext = AESGCM(key).decrypt(
                bytes.fromhex(header["nonce"]),
                ciphertext,
                header_bytes,
            )
        except Exception as error:
            raise ValueError("Backup password or authentication is invalid") from error
        bundle = temporary / "decrypted.aex"
        bundle.write_bytes(plaintext)
        return bundle

    @staticmethod
    def _validated_manifest(archive: zipfile.ZipFile) -> dict:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Archive contains duplicate entries")
        if "manifest.json" not in names:
            raise ValueError("Export manifest is missing")
        for name in names:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise ValueError(f"Unsafe archive path: {name}")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != EXPORT_FORMAT:
            raise ValueError("Unsupported AccountEval export format")
        if int(manifest.get("schema_version", 0)) > CURRENT_SCHEMA_VERSION:
            raise ValueError("Export requires a newer AccountEval schema")
        entries = [entry["path"] for entry in manifest.get("entries", [])]
        declared = set(entries)
        if len(entries) != len(declared):
            raise ValueError("Manifest contains duplicate entries")
        if set(names) != declared | {"manifest.json"}:
            raise ValueError("Archive content differs from manifest")
        return manifest

    @staticmethod
    def _safe_join(root: Path, relative: str) -> Path:
        target = root / relative
        if not PortabilityService._within(target.resolve(), root):
            raise ValueError(f"Unsafe target path: {relative}")
        return target

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _files(root: Path) -> list[Path]:
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _integrity_for_directory(cls, root: Path) -> dict:
        files = cls._files(root)
        return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}

"""Preview-gated, local-only document batch ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from account_eval.ledger.models import to_minor_units
from account_eval.ledger.repository import LedgerRepository

from .extraction import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    LocalOCRProvider,
    detect_mime_type,
    expected_mime_types,
    extract_metadata,
    json_metadata,
)
from .models import DocumentBatchExecution, DocumentBatchPreview, DocumentFilePreview


class DocumentBatchService:
    """Create an immutable file manifest, then ingest exactly that manifest."""

    def __init__(
        self,
        repository: LedgerRepository,
        storage_dir: str | Path,
        *,
        max_file_size_bytes: int = 25 * 1024 * 1024,
        ocr_provider: LocalOCRProvider | None = None,
    ) -> None:
        if max_file_size_bytes < 1:
            raise ValueError("max_file_size_bytes must be positive")
        self.repository = repository
        self.storage_dir = Path(storage_dir).resolve()
        self.max_file_size_bytes = max_file_size_bytes
        self.ocr_provider = ocr_provider

    def preview(self, path: str | Path) -> DocumentBatchPreview:
        source = Path(path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Document source does not exist: {source}")
        source_root = source if source.is_dir() else source.parent
        files = tuple(self._inspect_file(item, source_root) for item in self._source_files(source))
        payload = {
            "contract_version": 1,
            "max_file_size_bytes": self.max_file_size_bytes,
            "files": [item.as_dict() for item in files],
        }
        canonical = json.dumps(
            {"source_root": str(source_root), "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        preview_id = f"document-batch-preview-{uuid4()}"
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_document_batch_previews(
                       id, fingerprint, source_root, payload_json, status
                   ) VALUES (?, ?, ?, ?, 'prepared')""",
                (
                    preview_id,
                    fingerprint,
                    str(source_root),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.repository._audit(
                connection,
                "document_batch_preview",
                preview_id,
                "created",
                None,
                {
                    "fingerprint": fingerprint,
                    "files": len(files),
                    "valid_files": sum(item.valid for item in files),
                },
            )
        return self.get(preview_id)

    def get(self, preview_id: str) -> DocumentBatchPreview:
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_document_batch_previews WHERE id=?", (preview_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown document batch preview {preview_id}")
        payload = json.loads(row["payload_json"])
        files = tuple(self._file_from_payload(item) for item in payload.get("files", []))
        result = json.loads(row["result_json"] or "{}") or None
        return DocumentBatchPreview(
            preview_id=str(row["id"]),
            fingerprint=str(row["fingerprint"]),
            source_root=str(row["source_root"]),
            status=str(row["status"]),
            files=files,
            result=result,
        )

    def execute(self, preview_id: str, fingerprint: str) -> DocumentBatchExecution:
        preview = self.get(preview_id)
        if preview.status == "executed":
            if not preview.result:
                raise RuntimeError("Executed document preview has no result")
            return self._execution_from_payload(preview.result)
        if preview.status != "prepared":
            raise ValueError(f"Document batch preview is not executable: {preview.status}")
        if preview.fingerprint != fingerprint:
            raise ValueError("Document batch preview fingerprint does not match")
        try:
            self._verify_files(preview)
            return self._execute_verified(preview)
        except Exception as error:
            with self.repository.database.connect() as connection:
                connection.execute(
                    """UPDATE ledger_document_batch_previews
                       SET status='failed', result_json=? WHERE id=? AND status='prepared'""",
                    (
                        json.dumps(
                            {"error": type(error).__name__, "message": str(error)},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        preview_id,
                    ),
                )
            raise

    def _execute_verified(self, preview: DocumentBatchPreview) -> DocumentBatchExecution:
        valid_files = [item for item in preview.files if item.valid]
        invalid_errors = tuple(
            {"relative_path": item.relative_path, "error": "; ".join(item.errors)}
            for item in preview.files
            if not item.valid
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=".document-batch-", dir=self.storage_dir))
        staged: dict[str, Path] = {}
        moved: list[Path] = []
        try:
            for item in valid_files:
                if item.content_hash in staged:
                    continue
                target = stage_dir / item.content_hash
                shutil.copy2(item.source_path, target)
                if self._sha256(target) != item.content_hash:
                    raise ValueError(f"Document changed while staging: {item.relative_path}")
                staged[item.content_hash] = target

            created_documents = 0
            reused_documents = 0
            created_origins = 0
            results: list[dict[str, Any]] = []
            digest_states: dict[str, tuple[str, bool, Path]] = {}
            with self.repository.database.connect() as connection:
                claimed = connection.execute(
                    """UPDATE ledger_document_batch_previews SET status='executed'
                       WHERE id=? AND status='prepared' AND fingerprint=?""",
                    (preview.preview_id, preview.fingerprint),
                )
                if claimed.rowcount != 1:
                    raise ValueError("Document batch preview was claimed by another execution")
                for item in valid_files:
                    state = digest_states.get(item.content_hash)
                    if state is None:
                        row = connection.execute(
                            "SELECT * FROM ledger_documents WHERE content_hash=?",
                            (item.content_hash,),
                        ).fetchone()
                        extension = self._storage_extension(str(item.mime_type))
                        destination = (
                            self.storage_dir
                            / "content"
                            / item.content_hash[:2]
                            / f"{item.content_hash}{extension}"
                        )
                        if row is None:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if destination.exists():
                                if self._sha256(destination) != item.content_hash:
                                    raise ValueError(f"Storage collision for {item.content_hash}")
                            else:
                                os.replace(staged[item.content_hash], destination)
                                moved.append(destination)
                            document_id = f"document-{uuid4()}"
                            metadata = dict(item.metadata)
                            connection.execute(
                                """INSERT INTO ledger_documents(
                                       id, filename, document_date, amount_minor, merchant,
                                       reference, storage_path, content_hash, mime_type,
                                       extraction_status, metadata_json
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    document_id,
                                    item.original_filename,
                                    metadata.get("document_date"),
                                    self._amount_minor(metadata.get("amount")),
                                    metadata.get("merchant"),
                                    metadata.get("reference"),
                                    str(destination),
                                    item.content_hash,
                                    item.mime_type,
                                    item.extraction_status,
                                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                                ),
                            )
                            self.repository._audit(
                                connection,
                                "document",
                                document_id,
                                "batch_import",
                                None,
                                {
                                    "content_hash": item.content_hash,
                                    "mime_type": item.mime_type,
                                    "preview_id": preview.preview_id,
                                },
                            )
                            created_documents += 1
                            created = True
                        else:
                            document_id = str(row["id"])
                            created = False
                            reused_documents += 1
                            if not row["storage_path"]:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                if not destination.exists():
                                    os.replace(staged[item.content_hash], destination)
                                    moved.append(destination)
                                connection.execute(
                                    "UPDATE ledger_documents SET storage_path=? WHERE id=?",
                                    (str(destination), document_id),
                                )
                        state = (document_id, created, destination)
                        digest_states[item.content_hash] = state
                    document_id, created, destination = state
                    origin_key = hashlib.sha256(
                        f"{Path(item.source_path).resolve()}\0{item.content_hash}".encode()
                    ).hexdigest()
                    before_changes = connection.total_changes
                    connection.execute(
                        """INSERT INTO ledger_document_origins(
                               id, document_id, origin_key, relative_path,
                               original_filename, content_hash, mime_type,
                               size_bytes, mtime_ns
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(origin_key) DO NOTHING""",
                        (
                            f"document-origin-{uuid4()}",
                            document_id,
                            origin_key,
                            item.relative_path,
                            item.original_filename,
                            item.content_hash,
                            item.mime_type,
                            item.size_bytes,
                            item.mtime_ns,
                        ),
                    )
                    if connection.total_changes > before_changes:
                        created_origins += 1
                    results.append(
                        {
                            "document_id": document_id,
                            "relative_path": item.relative_path,
                            "content_hash": item.content_hash,
                            "storage_path": str(destination),
                            "created": created,
                        }
                    )
                execution = DocumentBatchExecution(
                    preview_id=preview.preview_id,
                    fingerprint=preview.fingerprint,
                    status="executed",
                    created_documents=created_documents,
                    reused_documents=reused_documents,
                    created_origins=created_origins,
                    documents=tuple(results),
                    errors=invalid_errors,
                )
                connection.execute(
                    """UPDATE ledger_document_batch_previews
                       SET result_json=?, source_root='', payload_json=?,
                           executed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        json.dumps(execution.as_dict(), ensure_ascii=False, sort_keys=True),
                        json.dumps(
                            {
                                "contract_version": 1,
                                "max_file_size_bytes": self.max_file_size_bytes,
                                "files": [
                                    item.as_dict() | {"source_path": ""} for item in preview.files
                                ],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        preview.preview_id,
                    ),
                )
                self.repository._audit(
                    connection,
                    "document_batch_preview",
                    preview.preview_id,
                    "executed",
                    None,
                    {
                        "fingerprint": preview.fingerprint,
                        "created_documents": created_documents,
                        "reused_documents": reused_documents,
                        "created_origins": created_origins,
                    },
                )
            return execution
        except Exception:
            for path in moved:
                path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _source_files(self, source: Path) -> list[Path]:
        if source.is_file():
            return [source]
        if not source.is_dir():
            raise ValueError(f"Document source is neither file nor directory: {source}")
        return sorted(
            item
            for item in source.rglob("*")
            if item.is_file() and item.suffix.casefold() in SUPPORTED_DOCUMENT_EXTENSIONS
        )

    def _inspect_file(self, path: Path, source_root: Path) -> DocumentFilePreview:
        errors: list[str] = []
        resolved = path.resolve()
        try:
            relative_path = str(resolved.relative_to(source_root.resolve()))
        except ValueError:
            relative_path = path.name
            errors.append("Document path escapes the selected source directory")
        stat = resolved.stat()
        if stat.st_size > self.max_file_size_bytes:
            errors.append(f"Document exceeds {self.max_file_size_bytes} bytes")
        content_hash = self._sha256(resolved)
        mime_type: str | None = None
        extraction_status = "not_extracted"
        metadata: dict[str, str] = {}
        try:
            mime_type = detect_mime_type(resolved)
            if mime_type not in expected_mime_types(resolved):
                errors.append("Filename extension does not match document content")
            elif not errors:
                extracted, extraction_status = extract_metadata(
                    resolved,
                    mime_type,
                    ocr_provider=self.ocr_provider,
                )
                metadata = json_metadata(extracted)
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
        return DocumentFilePreview(
            source_path=str(resolved),
            relative_path=relative_path,
            original_filename=resolved.name,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            content_hash=content_hash,
            mime_type=mime_type,
            extraction_status=extraction_status,
            metadata=metadata,
            errors=tuple(errors),
        )

    def _verify_files(self, preview: DocumentBatchPreview) -> None:
        for item in preview.files:
            if not item.valid:
                continue
            source = Path(item.source_path)
            if not source.is_file():
                raise ValueError(f"Document disappeared after preview: {item.relative_path}")
            stat = source.stat()
            if stat.st_size != item.size_bytes or stat.st_mtime_ns != item.mtime_ns:
                raise ValueError(f"Document changed after preview: {item.relative_path}")
            if self._sha256(source) != item.content_hash:
                raise ValueError(f"Document changed after preview: {item.relative_path}")
            if detect_mime_type(source) != item.mime_type:
                raise ValueError(f"Document type changed after preview: {item.relative_path}")

    @staticmethod
    def _file_from_payload(payload: dict[str, Any]) -> DocumentFilePreview:
        return DocumentFilePreview(
            source_path=str(payload["source_path"]),
            relative_path=str(payload["relative_path"]),
            original_filename=str(payload["original_filename"]),
            size_bytes=int(payload["size_bytes"]),
            mtime_ns=int(payload["mtime_ns"]),
            content_hash=str(payload["content_hash"]),
            mime_type=str(payload["mime_type"]) if payload.get("mime_type") else None,
            extraction_status=str(payload["extraction_status"]),
            metadata={str(key): str(value) for key, value in payload.get("metadata", {}).items()},
            errors=tuple(str(value) for value in payload.get("errors", [])),
        )

    @staticmethod
    def _execution_from_payload(payload: dict[str, Any]) -> DocumentBatchExecution:
        return DocumentBatchExecution(
            preview_id=str(payload["preview_id"]),
            fingerprint=str(payload["fingerprint"]),
            status=str(payload["status"]),
            created_documents=int(payload["created_documents"]),
            reused_documents=int(payload["reused_documents"]),
            created_origins=int(payload["created_origins"]),
            documents=tuple(payload.get("documents", [])),
            errors=tuple(payload.get("errors", [])),
        )

    @staticmethod
    def _amount_minor(value: str | None) -> int | None:
        return to_minor_units(Decimal(value)) if value is not None else None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _storage_extension(mime_type: str) -> str:
        return {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/tiff": ".tiff",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        }[mime_type]

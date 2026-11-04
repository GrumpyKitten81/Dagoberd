"""Immutable contracts for document preview and execution workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DocumentFilePreview:
    source_path: str
    relative_path: str
    original_filename: str
    size_bytes: int
    mtime_ns: int
    content_hash: str
    mime_type: str | None
    extraction_status: str
    metadata: dict[str, str]
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"valid": self.valid}


@dataclass(frozen=True, slots=True)
class DocumentBatchPreview:
    preview_id: str
    fingerprint: str
    source_root: str
    status: str
    files: tuple[DocumentFilePreview, ...]
    result: dict[str, Any] | None = None

    @property
    def valid_count(self) -> int:
        return sum(item.valid for item in self.files)

    @property
    def error_count(self) -> int:
        return len(self.files) - self.valid_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "fingerprint": self.fingerprint,
            "source_root": self.source_root,
            "status": self.status,
            "files": [item.as_dict() for item in self.files],
            "valid_count": self.valid_count,
            "error_count": self.error_count,
            "result": self.result,
            "requires_confirmation": self.status == "prepared",
        }


@dataclass(frozen=True, slots=True)
class DocumentBatchExecution:
    preview_id: str
    fingerprint: str
    status: str
    created_documents: int
    reused_documents: int
    created_origins: int
    documents: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

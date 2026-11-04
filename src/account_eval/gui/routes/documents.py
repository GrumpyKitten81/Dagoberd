"""Document upload and matching endpoints."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from account_eval.contracts import GUI_CONTRACT_VERSION, success
from account_eval.gui.context import GuiContext
from account_eval.gui.schemas import (
    DocumentAssignRequest,
    DocumentBatchRunRequest,
    DocumentMatchApplyRequest,
    MatchReviewRequest,
)

router = APIRouter(prefix="/api/documents")
UPLOAD_FILE = File(...)


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.post("/upload")
async def upload(request: Request, file: UploadFile = UPLOAD_FILE) -> dict:
    context = _context(request)
    filename = Path(file.filename or "document").name
    target = context.loaded.config.documents.storage_dir / f"{uuid4()}-{filename}"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if len(content) > context.loaded.config.imports.max_file_size_mib * 1024 * 1024:
        raise ValueError("Document file exceeds configured upload limit")
    target.write_bytes(content)
    return success(context.documents().import_file(target), contract_version=GUI_CONTRACT_VERSION)


@router.get("")
def documents(request: Request, status: str | None = None) -> dict:
    return success(
        _context(request).documents().list_documents(status=status),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/batch/preview")
def batch_preview(path: str, request: Request) -> dict:
    return success(
        _context(request).document_batches().preview(path).as_dict(),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/batch/{preview_id}/run")
def batch_run(preview_id: str, payload: DocumentBatchRunRequest, request: Request) -> dict:
    return success(
        _context(request).document_batches().execute(preview_id, payload.fingerprint).as_dict(),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/{document_id}/candidates")
def candidates(document_id: str, request: Request) -> dict:
    return success(
        _context(request).documents().suggest(document_id), contract_version=GUI_CONTRACT_VERSION
    )


@router.get("/{document_id}/preview-matches")
def preview_matches(document_id: str, request: Request, limit: int = 100) -> dict:
    return success(
        _context(request).documents().preview_matches(document_id, limit=limit),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/{document_id}/apply-matches")
def apply_matches(document_id: str, payload: DocumentMatchApplyRequest, request: Request) -> dict:
    return success(
        _context(request)
        .documents()
        .apply_match_preview(
            document_id,
            payload.fingerprint,
            payload.transaction_ids,
            expected_document_version=payload.expected_document_version,
            append=payload.append,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/matches/{match_id}/review")
def review(match_id: str, payload: MatchReviewRequest, request: Request) -> dict:
    return success(
        _context(request).documents().review(match_id, payload.decision, payload.expected_version),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/{document_id}/assign")
def assign(document_id: str, payload: DocumentAssignRequest, request: Request) -> dict:
    return success(
        _context(request)
        .documents()
        .assign(
            document_id,
            payload.transaction_id,
            expected_document_version=payload.expected_document_version,
            expected_transaction_version=payload.expected_transaction_version,
            append=payload.append,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/matches/{match_id}")
def unmatch(match_id: str, expected_version: int, request: Request) -> dict:
    return success(
        _context(request).documents().unmatch(match_id, expected_version),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/{document_id}/file")
def document_file(document_id: str, request: Request) -> FileResponse:
    document = _context(request).documents().get_document(document_id)
    return FileResponse(
        Path(document["storage_path"]),
        media_type=document.get("mime_type") or "application/octet-stream",
        filename=document["filename"],
    )

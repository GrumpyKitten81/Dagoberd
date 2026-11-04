"""Two-stage import endpoints."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel

from dagoberd.contracts import GUI_CONTRACT_VERSION, success
from dagoberd.gui.context import GuiContext

router = APIRouter(prefix="/api/imports")
UPLOAD_FILE = File(...)


class PreviewRequest(BaseModel):
    source: str
    profile: str | None = None
    account_id: str | None = None
    path: str


def _context(request: Request) -> GuiContext:
    return request.app.state.context


@router.post("/upload")
async def upload(request: Request, file: UploadFile = UPLOAD_FILE) -> dict:
    context = _context(request)
    content = await file.read()
    limit = context.loaded.config.imports.max_file_size_mib * 1024 * 1024
    if len(content) > limit:
        raise ValueError("Import file exceeds configured upload limit")
    filename = Path(file.filename or "import").name
    target = (
        (context.loaded.config.cloud.workspace or Path("output"))
        / "import-staging"
        / str(uuid4())
        / filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return success({"path": str(target)}, contract_version=GUI_CONTRACT_VERSION)


@router.post("/preview")
def preview(payload: PreviewRequest, request: Request) -> dict:
    return success(
        _context(request)
        .imports()
        .preview(payload.source, payload.path, payload.account_id, payload.profile)
        .as_dict(),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/{preview_id}/run")
def run(preview_id: str, request: Request) -> dict:
    return success(
        _context(request).imports().execute(preview_id).as_dict(),
        contract_version=GUI_CONTRACT_VERSION,
    )

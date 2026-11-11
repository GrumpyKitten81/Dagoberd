"""FastAPI application factory for the local GUI."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from dagoberd.config import LoadedConfig
from dagoberd.gui.context import GuiContext
from dagoberd.gui.errors import install_error_handlers
from dagoberd.gui.jobs import JobRegistry
from dagoberd.gui.routes import (
    admin,
    dashboard,
    documents,
    imports,
    meta,
    operations,
    transactions,
)
from dagoberd.gui.security import StaticAssetCacheMiddleware, TokenMiddleware
from dagoberd.gui.workspace_context import WorkspaceContextMiddleware
from dagoberd.ui import styles_directory
from dagoberd.workspaces import workspace_operation


def create_app(loaded: LoadedConfig | None, token: str) -> FastAPI:
    static_directory = Path(__file__).with_name("static")
    design_directory = styles_directory()
    asset_version = _static_asset_version(static_directory, design_directory)
    asset_base = f"/assets/{asset_version}"
    index = (
        (static_directory / "index.html")
        .read_text(encoding="utf-8")
        .replace("__DAGOBERD_ASSETS__", asset_base)
    )
    app = FastAPI(docs_url=None, redoc_url=None)
    if loaded is not None:
        with workspace_operation(loaded):
            app.state.context = GuiContext(loaded)
        if loaded.config.workspace.id is not None:
            app.add_middleware(WorkspaceContextMiddleware, loaded=loaded)
    app.state.jobs = JobRegistry()
    app.state.asset_version = asset_version
    app.add_middleware(TokenMiddleware, token=token)
    app.add_middleware(StaticAssetCacheMiddleware)
    install_error_handlers(app)
    for router in (
        meta.router,
        dashboard.router,
        transactions.router,
        admin.router,
        imports.router,
        documents.router,
        operations.router,
    ):
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    def gui_index() -> HTMLResponse:
        return HTMLResponse(index)

    app.mount(f"{asset_base}/design", StaticFiles(directory=design_directory), name="design")
    app.mount(asset_base, StaticFiles(directory=static_directory), name="static")
    return app


def _static_asset_version(*directories: Path) -> str:
    """Return a stable URL namespace for the exact frontend source tree."""
    digest = hashlib.sha256()
    for directory in directories:
        digest.update(directory.name.encode())
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]

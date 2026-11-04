from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from dagoberd.config import LoadedConfig
from dagoberd.contracts import GUI_CONTRACT_VERSION, failure
from dagoberd.workspaces import CURRENT_WORKSPACE, workspace_operation


class WorkspaceContextMiddleware:
    def __init__(self, app: ASGIApp, loaded: LoadedConfig) -> None:
        self.app = app
        self.loaded = loaded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if CURRENT_WORKSPACE.get() == self.loaded:
            await self.app(scope, receive, send)
            return
        try:
            with workspace_operation(self.loaded):
                await self.app(scope, receive, send)
        except (ValueError, FileNotFoundError) as error:
            await JSONResponse(
                failure("invalid_workspace", str(error), contract_version=GUI_CONTRACT_VERSION),
                status_code=400,
            )(scope, receive, send)

from __future__ import annotations

import argparse
import secrets
import webbrowser
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

from dagoberd.config import LoadedConfig, StrictModel
from dagoberd.contracts import GUI_CONTRACT_VERSION, failure, success
from dagoberd.gui.app import create_app
from dagoberd.gui.jobs import JobRegistry
from dagoberd.gui.security import ensure_loopback, ensure_port_available
from dagoberd.workspace_cli import adopt_credentials
from dagoberd.workspace_git import WorkspaceGit, adopt_git, clone_workspace
from dagoberd.workspace_status import workspace_status
from dagoberd.workspaces import WorkspaceRegistry, workspace_operation


class WorkspaceAction(StrictModel):
    action: str
    name: str = ""
    path: str = ""
    remote: str = ""
    cloud_root: str | None = None
    files: list[str] = Field(default_factory=list)
    message: str = ""
    preview_id: str = ""
    fingerprint: str = ""
    confirmed: bool = False
    snapshot: str = ""


def workspace_action(registry: WorkspaceRegistry, payload: WorkspaceAction) -> Any:
    if payload.action == "status":
        return workspace_status(registry, payload.name)
    if payload.action.startswith("sync-") or payload.action in {
        "snapshot-inspect",
        "snapshot-adopt",
    }:
        from dagoberd.workspace_sync import WorkspaceSync, adopt_snapshot, inspect_snapshot

        if payload.action == "snapshot-inspect":
            return inspect_snapshot(payload.snapshot)
        if payload.action == "snapshot-adopt":
            return adopt_snapshot(
                registry,
                payload.name,
                payload.path,
                payload.snapshot,
                payload.fingerprint,
                yes=payload.confirmed,
            )
        sync = WorkspaceSync(registry, payload.name)
        if payload.action == "sync-status":
            return sync.status()
        if payload.action in {"sync-configure", "sync-disconnect"}:
            if payload.action == "sync-configure" and not payload.cloud_root:
                raise ValueError("Private snapshot folder is required")
            return sync.configure(
                payload.cloud_root if payload.action == "sync-configure" else None,
                yes=payload.confirmed,
            )
        if payload.action == "sync-publish":
            return sync.preview("publish")
        if payload.action == "sync-restore":
            return sync.preview("restore")
        if payload.action == "sync-run":
            return sync.run(payload.preview_id, payload.fingerprint, yes=payload.confirmed)
        raise ValueError("Unknown snapshot action")
    if payload.action == "add":
        return registry.add(payload.name, payload.path)
    if payload.action in {"init", "create"}:
        return registry.initialize(
            payload.name,
            payload.path,
            create=payload.action == "create",
            cloud_root=payload.cloud_root,
        )
    if payload.action == "clone":
        return clone_workspace(
            registry, payload.name, payload.remote, payload.path, cloud_root=payload.cloud_root
        )
    if payload.action == "remove":
        registry.remove(payload.name)
        return {"removed": payload.name, "files_preserved": True}
    if payload.action == "adopt-credentials":
        return adopt_credentials(registry, payload.name, payload.files, yes=payload.confirmed)
    if payload.action in {"cloud-preview", "cloud-run", "cloud-recover"}:
        from dagoberd.workspace_cloud import WorkspaceCloud, recover_cloud

        if payload.action == "cloud-recover":
            return recover_cloud(registry, payload.name, yes=payload.confirmed)
        cloud = WorkspaceCloud(registry, payload.name)
        if payload.action == "cloud-preview":
            if not payload.cloud_root:
                raise ValueError("Cloud root is required")
            return cloud.preview(payload.cloud_root)
        return cloud.run(payload.preview_id, payload.fingerprint, yes=payload.confirmed)
    if payload.action == "git-init":
        return adopt_git(registry, payload.name, yes=payload.confirmed)
    service = WorkspaceGit(registry, payload.name)
    if payload.action in {"git-remote", "git-disconnect"}:
        if payload.action == "git-remote" and not payload.remote:
            raise ValueError("Remote URL is required")
        return service.configure_remote(
            payload.remote if payload.action == "git-remote" else None, yes=payload.confirmed
        )
    if payload.action == "git-status":
        return service.status()
    if payload.action == "git-trust":
        return service.trust(yes=payload.confirmed)
    if payload.action in {"git-pull", "git-commit", "git-push"}:
        return service.preview(payload.action.removeprefix("git-"), payload.files, payload.message)
    if payload.action == "git-run":
        return service.run(payload.preview_id, payload.fingerprint, yes=payload.confirmed)
    raise ValueError("Unknown workspace action")


class WorkspaceDispatch:
    def __init__(
        self, app: ASGIApp, registry: WorkspaceRegistry, token: str, legacy: bool = False
    ) -> None:
        self.app = app
        self.registry = registry
        self.token = token
        self.legacy = legacy
        self.jobs: dict[str, JobRegistry] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        if path in {"/api/workspaces", "/api/workspace-actions"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        if not secrets.compare_digest(
            headers.get(b"x-accounteval-token", b""), self.token.encode()
        ):
            await JSONResponse(
                failure(
                    "unauthorized",
                    "Missing or invalid GUI token",
                    contract_version=GUI_CONTRACT_VERSION,
                ),
                status_code=401,
            )(scope, receive, send)
            return
        parts = path.split("/", 4)
        if len(parts) != 5 or parts[2] != "workspaces":
            if self.legacy and not self.registry.read().workspaces:
                await self.app(scope, receive, send)
                return
            await JSONResponse(
                failure(
                    "workspace_required",
                    "Select a workspace",
                    contract_version=GUI_CONTRACT_VERSION,
                ),
                status_code=400,
            )(scope, receive, send)
            return
        try:
            loaded = self.registry.load(parts[3])
            with workspace_operation(loaded, self.registry):
                application = create_app(loaded, self.token)
                application.state.workspace_registry = self.registry
                application.state.jobs = self.jobs.setdefault(
                    str(loaded.config.workspace.id), JobRegistry()
                )
                nested = {
                    **scope,
                    "path": f"/api/{parts[4]}",
                    "raw_path": f"/api/{parts[4]}".encode(),
                }
                await application(nested, receive, send)
        except (KeyError, ValueError, FileNotFoundError) as error:
            await JSONResponse(
                failure("invalid_workspace", str(error), contract_version=GUI_CONTRACT_VERSION),
                status_code=400,
            )(scope, receive, send)


def create_workspace_app(
    registry: WorkspaceRegistry, token: str, standalone: LoadedConfig | None = None
) -> FastAPI:
    app = create_app(standalone, token)
    router = APIRouter(prefix="/api")

    @router.get("/workspaces")
    def list_workspaces() -> dict[str, Any]:
        return success(
            {
                "workspaces": registry.list(),
                "legacy": standalone is not None and not registry.read().workspaces,
            },
            contract_version=GUI_CONTRACT_VERSION,
        )

    @router.post("/workspace-actions")
    def manage_workspace(payload: WorkspaceAction) -> dict[str, Any]:
        return success(workspace_action(registry, payload), contract_version=GUI_CONTRACT_VERSION)

    app.include_router(router)
    app.add_middleware(
        WorkspaceDispatch, registry=registry, token=token, legacy=standalone is not None
    )
    return app


def launch_workspace_gui(
    args: argparse.Namespace, registry: WorkspaceRegistry, selected: LoadedConfig | None
) -> int:
    import uvicorn

    host = ensure_loopback(args.host)
    if not 1 <= args.port <= 65535:
        raise ValueError("GUI port must be between 1 and 65535")
    ensure_port_available(host, args.port)
    token = secrets.token_urlsafe(32)
    workspace = (
        str(selected.config.workspace.id) if selected and selected.config.workspace.id else ""
    )
    address = f"http://{host}:{args.port}/?workspace={workspace}#t={token}"
    print(address, flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    standalone = selected if selected and selected.config.workspace.id is None else None
    uvicorn.run(
        create_workspace_app(registry, token, standalone),
        host=host,
        port=args.port,
        log_level="warning",
    )
    return 0

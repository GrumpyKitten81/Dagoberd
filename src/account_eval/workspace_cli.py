from __future__ import annotations

import argparse
from typing import Any

from account_eval.workspace_git import WorkspaceGit, clone_workspace
from account_eval.workspaces import WorkspaceRegistry


def add_workspace_commands(commands: Any) -> None:
    parser = commands.add_parser("workspaces", help="Manage isolated local workspaces")
    subcommands = parser.add_subparsers(dest="workspace_command", required=True)
    subcommands.add_parser("list")
    for command in ("add", "create", "init", "clone"):
        child = subcommands.add_parser(command)
        child.add_argument("name")
        child.add_argument("path")
        if command == "clone":
            child.add_argument("--remote", required=True)
        if command != "add":
            child.add_argument("--cloud-root")
    for command in ("remove", "doctor"):
        child = subcommands.add_parser(command)
        child.add_argument("name")
    credentials = subcommands.add_parser("adopt-credentials")
    credentials.add_argument("name")
    credentials.add_argument("--secret", action="append", required=True)
    credentials.add_argument("--yes", action="store_true")
    cloud = subcommands.add_parser("cloud")
    cloud.add_argument("name")
    cloud_actions = cloud.add_subparsers(dest="cloud_command", required=True)
    cloud_preview = cloud_actions.add_parser("preview")
    cloud_preview.add_argument("--root", required=True)
    cloud_run = cloud_actions.add_parser("run")
    cloud_run.add_argument("preview_id")
    cloud_run.add_argument("--fingerprint", required=True)
    cloud_run.add_argument("--yes", action="store_true")
    cloud_recover = cloud_actions.add_parser("recover")
    cloud_recover.add_argument("--yes", action="store_true")
    git = subcommands.add_parser("git")
    git.add_argument("name")
    actions = git.add_subparsers(dest="git_command", required=True)
    actions.add_parser("status")
    trust = actions.add_parser("trust")
    trust.add_argument("--yes", action="store_true")
    preview = actions.add_parser("preview")
    preview.add_argument("action", choices=("pull", "commit", "push"))
    preview.add_argument("--file", action="append", default=[])
    preview.add_argument("--message", default="")
    run = actions.add_parser("run")
    run.add_argument("preview_id")
    run.add_argument("--fingerprint", required=True)
    run.add_argument("--yes", action="store_true")


def handle_workspace_command(args: argparse.Namespace, registry: WorkspaceRegistry) -> Any:
    command = args.workspace_command
    if command == "list":
        return registry.list()
    if command == "add":
        return registry.add(args.name, args.path)
    if command in {"create", "init"}:
        return registry.initialize(
            args.name, args.path, create=command == "create", cloud_root=args.cloud_root
        )
    if command == "clone":
        return clone_workspace(
            registry, args.name, args.remote, args.path, cloud_root=args.cloud_root
        )
    if command == "remove":
        registry.remove(args.name)
        return {"removed": args.name, "files_preserved": True}
    if command == "doctor":
        loaded = registry.load(args.name)
        return {
            "workspace_id": str(loaded.config.workspace.id),
            "valid": True,
            "database_exists": loaded.config.database.path.is_file(),
            "cloud_workspace": str(loaded.config.cloud.workspace)
            if loaded.config.cloud.workspace
            else None,
        }
    if command == "adopt-credentials":
        return adopt_credentials(registry, args.name, args.secret, yes=args.yes)
    if command == "cloud":
        from account_eval.workspace_cloud import WorkspaceCloud, recover_cloud

        if args.cloud_command == "recover":
            return recover_cloud(registry, args.name, yes=args.yes)
        cloud = WorkspaceCloud(registry, args.name)
        if args.cloud_command == "preview":
            return cloud.preview(args.root)
        return cloud.run(args.preview_id, args.fingerprint, yes=args.yes)
    service = WorkspaceGit(registry, args.name)
    if args.git_command == "status":
        return service.status()
    if args.git_command == "trust":
        return service.trust(yes=args.yes)
    if args.git_command == "preview":
        return service.preview(args.action, args.file, args.message)
    return service.run(args.preview_id, args.fingerprint, yes=args.yes)


def adopt_credentials(
    registry: WorkspaceRegistry, selector: str, names: list[str], *, yes: bool
) -> dict[str, Any]:
    from account_eval.secrets import SecretStore
    from account_eval.workspaces import CURRENT_WORKSPACE, workspace_operation

    if not yes or any("session" in name.lower() for name in names):
        raise ValueError("Credential adoption requires --yes; bank sessions cannot be adopted")
    token = CURRENT_WORKSPACE.set(None)
    try:
        source = SecretStore()
    finally:
        CURRENT_WORKSPACE.reset(token)
    with workspace_operation(registry.load(selector), registry):
        target = SecretStore()
        for name in names:
            value = source.get_keyring(name)
            if not value:
                raise ValueError("Selected credential does not exist in the legacy keyring")
            if target.get_keyring(name):
                raise ValueError("Target credential already exists")
            target.set(name, value)
    return {"workspace_id": registry.entry(selector).id, "adopted": names}

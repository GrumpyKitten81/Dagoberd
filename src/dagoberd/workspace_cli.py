from __future__ import annotations

import argparse
from typing import Any

from dagoberd.workspace_git import WorkspaceGit, adopt_git, clone_workspace
from dagoberd.workspace_status import workspace_status
from dagoberd.workspaces import WorkspaceRegistry


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
    for command in ("remove", "doctor", "status"):
        child = subcommands.add_parser(command)
        child.add_argument("name")
    inspect = subcommands.add_parser("snapshot-inspect")
    inspect.add_argument("snapshot")
    adopt = subcommands.add_parser("snapshot-adopt")
    adopt.add_argument("name")
    adopt.add_argument("path")
    adopt.add_argument("--snapshot", required=True)
    adopt.add_argument("--fingerprint", required=True)
    adopt.add_argument("--yes", action="store_true")
    sync = subcommands.add_parser("sync", help="Exchange verified snapshots without Git")
    sync.add_argument("name")
    sync_actions = sync.add_subparsers(dest="sync_command", required=True)
    sync_actions.add_parser("status")
    configure = sync_actions.add_parser("configure")
    configure.add_argument("--root", required=True)
    configure.add_argument("--yes", action="store_true")
    disconnect = sync_actions.add_parser("disconnect")
    disconnect.add_argument("--yes", action="store_true")
    sync_preview = sync_actions.add_parser("preview")
    sync_preview.add_argument("action", choices=("publish", "restore"))
    sync_run = sync_actions.add_parser("run")
    sync_run.add_argument("preview_id")
    sync_run.add_argument("--fingerprint", required=True)
    sync_run.add_argument("--yes", action="store_true")
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
    init = actions.add_parser("init", help="Add local Git without a remote or initial commit")
    init.add_argument("--yes", action="store_true")
    remote = actions.add_parser("remote", help="Set or disconnect the optional origin")
    destination = remote.add_mutually_exclusive_group(required=True)
    destination.add_argument("--url")
    destination.add_argument("--remove", action="store_true")
    remote.add_argument("--yes", action="store_true")
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
    if command in {"doctor", "status"}:
        return workspace_status(registry, args.name)
    if command in {"snapshot-inspect", "snapshot-adopt", "sync"}:
        from dagoberd.workspace_sync import WorkspaceSync, adopt_snapshot, inspect_snapshot

        if command == "snapshot-inspect":
            return inspect_snapshot(args.snapshot)
        if command == "snapshot-adopt":
            return adopt_snapshot(
                registry, args.name, args.path, args.snapshot, args.fingerprint, yes=args.yes
            )
        sync = WorkspaceSync(registry, args.name)
        if args.sync_command == "status":
            return sync.status()
        if args.sync_command in {"configure", "disconnect"}:
            return sync.configure(
                args.root if args.sync_command == "configure" else None, yes=args.yes
            )
        if args.sync_command == "preview":
            return sync.preview(args.action)
        return sync.run(args.preview_id, args.fingerprint, yes=args.yes)
    if command == "adopt-credentials":
        return adopt_credentials(registry, args.name, args.secret, yes=args.yes)
    if command == "cloud":
        from dagoberd.workspace_cloud import WorkspaceCloud, recover_cloud

        if args.cloud_command == "recover":
            return recover_cloud(registry, args.name, yes=args.yes)
        cloud = WorkspaceCloud(registry, args.name)
        if args.cloud_command == "preview":
            return cloud.preview(args.root)
        return cloud.run(args.preview_id, args.fingerprint, yes=args.yes)
    if args.git_command == "init":
        return adopt_git(registry, args.name, yes=args.yes)
    service = WorkspaceGit(registry, args.name)
    if args.git_command == "remote":
        return service.configure_remote(args.url, yes=args.yes)
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
    from dagoberd.secrets import SecretStore
    from dagoberd.workspaces import CURRENT_WORKSPACE, workspace_operation

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

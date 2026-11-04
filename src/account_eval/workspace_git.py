from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from account_eval.workspaces import (
    WorkspaceRegistry,
    atomic_text,
    workspace_operation,
)


def validate_remote(remote: str) -> str:
    if not remote or remote.startswith("-") or "\n" in remote or "\r" in remote:
        raise ValueError("Invalid Git remote")
    if "://" in remote:
        parsed = urlsplit(remote)
        if parsed.scheme not in {"https", "ssh", "file"}:
            raise ValueError("Use an HTTPS, SSH or local Git remote")
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "https" and parsed.username)
        ):
            raise ValueError("Git remote URLs must not contain credentials, queries or fragments")
    elif "::" in remote:
        raise ValueError("External Git remote helpers are unavailable")
    return remote


def remote_label(remote: str) -> str:
    if "://" not in remote:
        return remote
    parsed = urlsplit(remote)
    return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))


def git(root: Path, *arguments: str, check: bool = True) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git is not installed; local workspace operations remain available")
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    for key in list(environment):
        if key.startswith("GIT_") and key not in {
            "GIT_TERMINAL_PROMPT",
            "GIT_OPTIONAL_LOCKS",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
        }:
            del environment[key]
    filter_options: list[str] = []
    if arguments:
        configured = subprocess.run(
            [
                executable,
                "-C",
                str(root),
                "config",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process|required)$",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
        )
        drivers = {key.rsplit(".", 1)[0] for key in configured.stdout.splitlines()}
        for driver in sorted(drivers):
            for option in ("clean", "smudge", "process"):
                filter_options.extend(["-c", f"{driver}.{option}="])
            filter_options.extend(["-c", f"{driver}.required=false"])
    result = subprocess.run(
        [
            executable,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.recurseSubmodules=false",
            *filter_options,
            "-C",
            str(root),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
    )
    if check and result.returncode:
        raise ValueError("Git operation failed; inspect the repository locally")
    return result.stdout.rstrip("\n")


def clone_workspace(
    registry: WorkspaceRegistry,
    name: str,
    remote: str,
    destination: str,
    *,
    cloud_root: str | None = None,
) -> dict[str, Any]:
    validate_remote(remote)
    target = Path(destination).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Clone destination must be a new or empty directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    git(target.parent, "clone", "--", remote, str(target))
    return registry.initialize(name, target, cloud_root=cloud_root)


class WorkspaceGit:
    def __init__(self, registry: WorkspaceRegistry, selector: str) -> None:
        self.registry = registry
        self.entry = registry.entry(selector)
        self.loaded = registry.load(selector)
        self.root = self.loaded.project_path.parent.resolve()
        if Path(git(self.root, "rev-parse", "--show-toplevel")).resolve() != self.root:
            raise ValueError("Workspace configuration must be at the repository root")

    def _remote(self) -> str:
        return validate_remote(git(self.root, "remote", "get-url", "origin"))

    def trust(self, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("Confirm that origin is a private repository with --yes")
        remote = self._remote()
        from account_eval.workspace_lock import file_lock

        with file_lock(self.registry.path.with_suffix(".lock"), exclusive=True):
            data = self.registry.read()
            for entry in data.workspaces:
                if entry.id == self.entry.id:
                    entry.private_remote = hashlib.sha256(remote.encode()).hexdigest()
            self.registry._write(data)
        return {"workspace_id": self.entry.id, "private_origin": remote_label(remote)}

    def status(self) -> dict[str, Any]:
        return {
            "workspace_id": self.entry.id,
            "branch": git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False),
            "head": git(self.root, "rev-parse", "--verify", "HEAD", check=False),
            "origin": remote_label(self._remote()),
            "changes": git(self.root, "status", "--short"),
        }

    def _files(self, files: list[str]) -> list[str]:
        normalized = []
        for value in files:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("Select individual repository-relative files")
            target = self.root / path
            parts = path.parts
            if any(
                part.lower().startswith(".env")
                or part.lower()
                in {
                    ".git",
                    ".account-eval",
                    "account-eval.local.toml",
                    "workspaces.toml",
                }
                or part.lower().endswith((".lock", "-wal", "-shm", "-journal", ".pem", ".key"))
                for part in parts
            ):
                raise ValueError("Secrets, local settings and runtime files cannot be committed")
            if target.is_symlink() or not target.resolve().is_relative_to(self.root):
                raise ValueError("Selected file escapes the workspace repository")
            if target.exists() and not target.is_file():
                raise ValueError("Select individual files, not directories")
            if target.exists():
                content = target.read_bytes()
                if re.search(rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----", content):
                    raise ValueError("Selected file contains a private key")
            normalized.append(path.as_posix())
        return sorted(set(normalized))

    def _checkpoint(self, files: list[str]) -> None:
        database = self.loaded.config.database.path.resolve()
        if (
            not database.is_relative_to(self.root)
            or database.relative_to(self.root).as_posix() not in files
        ):
            return
        if not database.is_file():
            raise ValueError("Selected ledger does not exist")
        connection = sqlite3.connect(database, timeout=0)
        try:
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise ValueError("Ledger has active database readers or writers")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Ledger integrity check failed")
        finally:
            connection.close()

    def _snapshot(self, action: str, files: list[str], message: str) -> dict[str, Any]:
        remote = self._remote()
        if (
            action in {"commit", "push"}
            and self.registry.entry(self.entry.id).private_remote
            != hashlib.sha256(remote.encode()).hexdigest()
        ):
            raise ValueError("Confirm the private origin with workspaces git trust --yes first")
        branch = git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if not branch:
            raise ValueError("Git action requires a named branch")
        if action == "pull" and git(self.root, "status", "--porcelain"):
            raise ValueError("Pull requires a clean working tree")
        if action == "pull" and (
            git(self.root, "config", "--get", f"branch.{branch}.remote", check=False) != "origin"
            or not git(self.root, "rev-parse", "--verify", "@{upstream}", check=False)
        ):
            raise ValueError("Pull requires an upstream branch on origin")
        staged = git(
            self.root, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only", "-z"
        ).split("\0")
        if action == "commit" and set(filter(None, staged)) - set(files):
            raise ValueError("Staged changes must all be selected in the preview")
        return {
            "workspace_id": self.entry.id,
            "action": action,
            "files": files,
            "message": message,
            "branch": branch,
            "origin": remote_label(remote),
            "remote_hash": hashlib.sha256(remote.encode()).hexdigest(),
            "head": git(self.root, "rev-parse", "--verify", "HEAD", check=False),
            "index": hashlib.sha256(
                git(
                    self.root, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--binary"
                ).encode()
            ).hexdigest(),
            "upstream": git(self.root, "rev-parse", "--verify", "@{upstream}", check=False),
            "status": git(self.root, "status", "--porcelain", "-z"),
            "contents": {
                file: hashlib.sha256((self.root / file).read_bytes()).hexdigest()
                if (self.root / file).is_file()
                else None
                for file in files
            },
        }

    def preview(
        self, action: str, files: list[str] | None = None, message: str = ""
    ) -> dict[str, Any]:
        if action not in {"pull", "commit", "push"}:
            raise ValueError("Unknown Git action")
        selected = self._files(files or [])
        if action == "commit" and (not selected or not message.strip()):
            raise ValueError("Commit requires selected files and a message")
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            self._checkpoint(selected)
            if action == "pull":
                git(self.root, "fetch", "origin")
            payload = self._snapshot(action, selected, message)
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            identifier = str(uuid4())
            atomic_text(
                self.registry.path.parent / "git-previews" / f"{identifier}.json",
                json.dumps({"payload": payload, "fingerprint": fingerprint}),
            )
            return {
                "preview_id": identifier,
                "fingerprint": fingerprint,
                **{
                    key: payload[key]
                    for key in (
                        "workspace_id",
                        "action",
                        "files",
                        "message",
                        "branch",
                        "origin",
                        "head",
                    )
                },
            }

    def run(self, preview_id: str, fingerprint: str, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("Git execution requires local confirmation with --yes")
        from uuid import UUID

        path = self.registry.path.parent / "git-previews" / f"{UUID(preview_id)}.json"
        preview = json.loads(path.read_text(encoding="utf-8"))
        payload = preview["payload"]
        if payload["workspace_id"] != self.entry.id:
            raise ValueError("Git preview belongs to another workspace")
        with workspace_operation(self.loaded, self.registry, exclusive=True):
            self._checkpoint(payload["files"])
            if payload["action"] == "pull":
                git(self.root, "fetch", "origin")
            current = self._snapshot(
                payload["action"], self._files(payload["files"]), payload["message"]
            )
            actual = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
            if fingerprint != preview["fingerprint"] or fingerprint != actual:
                raise ValueError("Git state changed; create a new preview")
            path.unlink()
            if payload["action"] == "commit":
                git(self.root, "add", "--", *payload["files"])
                git(self.root, "commit", "-m", payload["message"])
            elif payload["action"] == "pull":
                git(self.root, "merge", "--ff-only", "@{upstream}")
                self.registry.refresh_storage(self.entry.id)
            else:
                push_url = git(self.root, "remote", "get-url", "--push", "origin")
                if push_url != self._remote():
                    raise ValueError("Push URL differs from the confirmed origin")
                git(self.root, "push", "origin", f"HEAD:refs/heads/{payload['branch']}")
            return self.status()

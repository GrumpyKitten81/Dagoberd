import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from dagoberd.cli import main
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.safe_files import prepared_new_file
from dagoberd.services.portability import PortabilityService

FORMATS = ("sqlite", "json", "portable", "encrypted")
PASSWORD = "release-test-password"


@pytest.fixture
def workspace(tmp_path):
    database = LedgerDatabase(tmp_path / "ledger.db")
    database.initialize()
    with closing(database.connect()) as connection, connection:
        connection.execute(
            """INSERT INTO ledger_providers(id, kind, display_name)
               VALUES ('before', 'test', 'Before')"""
        )
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "invoice.pdf").write_bytes(b"not-a-real-pdf")
    config = tmp_path / "account-eval.toml"
    config.write_text("[database]\npath='ledger.db'\n", encoding="utf-8")
    return PortabilityService(database.path, documents, tmp_path / "output", config)


def create_backup(service: PortabilityService, kind: str, target: Path):
    if kind == "sqlite":
        return service.database.backup(target)
    if kind == "json":
        return service.database.export_json(target)
    if kind == "portable":
        return service.export(target)
    return service.encrypted_backup(target, PASSWORD)


def assert_no_staging(root: Path) -> None:
    assert not list(root.rglob(".account-eval-write-*"))


@pytest.mark.parametrize("kind", FORMATS)
def test_backup_is_protected_from_creation_and_verified_before_final_name(
    workspace, tmp_path, monkeypatch, kind
):
    target = tmp_path / "backups" / kind
    original_open = os.open
    original_link = os.link
    protected_files = []
    linked = []
    old_umask = os.umask(0) if os.name != "nt" else None

    def observe_creation(path, flags, mode=0o777, **kwargs):
        descriptor = original_open(path, flags, mode, **kwargs)
        if Path(path).name == "payload" and Path(path).parent.name.startswith(
            ".account-eval-write-"
        ):
            protected_files.append(Path(path))
            if os.name != "nt":
                assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
        return descriptor

    def observe_link(staged, destination, **kwargs):
        destination = Path(destination)
        if destination == target:
            assert not target.exists()
            if kind == "sqlite":
                with closing(sqlite3.connect(staged)) as connection:
                    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert connection.execute("SELECT id FROM ledger_providers").fetchall() == [
                        ("before",)
                    ]
            elif kind == "json":
                assert (
                    json.loads(Path(staged).read_text())["tables"]["ledger_providers"][0]["id"]
                    == "before"
                )
            else:
                assert (
                    workspace.verify(staged, password=PASSWORD if kind == "encrypted" else None)[
                        "valid"
                    ]
                    is True
                )
            linked.append(destination)
        return original_link(staged, destination, **kwargs)

    monkeypatch.setattr(os, "open", observe_creation)
    monkeypatch.setattr(os, "link", observe_link)
    before = workspace.database.path.read_bytes()
    try:
        result = create_backup(workspace, kind, target)
    finally:
        if old_umask is not None:
            os.umask(old_umask)

    assert protected_files
    assert linked == [target]
    assert target.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if kind in {"portable", "encrypted"}:
        assert result["path"] == str(target)
        assert result["bundle_sha256"] == workspace._hash(target)
    assert workspace.database.path.read_bytes() == before
    assert_no_staging(tmp_path)


@pytest.mark.parametrize("kind", FORMATS)
def test_existing_backup_is_never_replaced(workspace, tmp_path, kind):
    target = tmp_path / "existing"
    target.write_bytes(b"changed")
    before = workspace.database.path.read_bytes()

    with pytest.raises(FileExistsError, match="already exists"):
        create_backup(workspace, kind, target)

    assert target.read_bytes() == b"changed"
    assert workspace.database.path.read_bytes() == before
    assert_no_staging(tmp_path)


def test_competing_creators_cannot_overwrite_each_other(tmp_path):
    target = tmp_path / "backup"
    with (
        pytest.raises(FileExistsError, match="already exists"),
        prepared_new_file(target) as first,
    ):
        first.write_bytes(b"changed")
        with prepared_new_file(target) as second:
            second.write_bytes(b"not-a-real-pdf")

    assert target.read_bytes() == b"not-a-real-pdf"
    assert_no_staging(tmp_path)


@pytest.mark.parametrize("kind", FORMATS)
@pytest.mark.parametrize("failure", ["sync", "link"])
def test_sync_and_link_failures_do_not_leave_final_files(
    workspace, tmp_path, monkeypatch, kind, failure
):
    target = tmp_path / "backup"
    before = workspace.database.path.read_bytes()
    sync = os.fsync
    link = os.link
    failed = False

    def fail_sync(descriptor):
        nonlocal failed
        if list(tmp_path.glob(".account-eval-write-*/payload")):
            failed = True
            raise OSError("Injected filesystem failure")
        return sync(descriptor)

    def fail_link(source, destination, **kwargs):
        nonlocal failed
        if Path(destination) == target:
            failed = True
            raise OSError("Injected filesystem failure")
        return link(source, destination, **kwargs)

    if failure == "sync":
        monkeypatch.setattr(os, "fsync", fail_sync)
    else:
        monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="filesystem failure"):
        create_backup(workspace, kind, target)

    assert failed
    assert not target.exists()
    assert workspace.database.path.read_bytes() == before
    assert_no_staging(tmp_path)


@pytest.mark.parametrize("kind", FORMATS)
def test_partial_write_failures_leave_no_backup(workspace, tmp_path, monkeypatch, kind):
    target = tmp_path / "backup"
    before = workspace.database.path.read_bytes()
    if kind == "sqlite":
        connection = workspace.database._read_only_connection()

        class InterruptedSource:
            def backup(self, destination):
                connection.backup(destination)
                raise OSError("Injected partial write failure")

            def close(self):
                connection.close()

        monkeypatch.setattr(workspace.database, "_read_only_connection", InterruptedSource)
    elif kind == "json":
        write_text = Path.write_text

        def partial_json(path, content, **kwargs):
            write_text(path, content[: len(content) // 2], **kwargs)
            raise OSError("Injected partial write failure")

        monkeypatch.setattr(Path, "write_text", partial_json)
    elif kind == "portable":
        write = zipfile.ZipFile.write

        def partial_archive(archive, filename, *args, **kwargs):
            write(archive, filename, *args, **kwargs)
            raise OSError("Injected partial write failure")

        monkeypatch.setattr(zipfile.ZipFile, "write", partial_archive)
    else:
        original_open = Path.open

        class InterruptedWriter:
            def __init__(self, handle):
                self.handle = handle

            def write(self, payload):
                self.handle.write(payload)
                raise OSError("Injected partial write failure")

        @contextmanager
        def partial_encrypted(path, *args, **kwargs):
            with original_open(path, *args, **kwargs) as handle:
                if args == ("wb",) and path.name == "payload":
                    yield InterruptedWriter(handle)
                else:
                    yield handle

        monkeypatch.setattr(Path, "open", partial_encrypted)
    with pytest.raises(OSError, match="partial write failure"):
        create_backup(workspace, kind, target)

    assert not target.exists()
    assert workspace.database.path.read_bytes() == before
    assert_no_staging(tmp_path)


@pytest.mark.parametrize("kind", FORMATS)
def test_verification_failure_prevents_final_file(workspace, tmp_path, monkeypatch, kind):
    target = tmp_path / "backup"
    if kind == "sqlite":
        original_connect = sqlite3.connect

        class InvalidIntegrity(sqlite3.Connection):
            def execute(self, statement, *args, **kwargs):
                if statement == "PRAGMA integrity_check":
                    return super().execute("SELECT 'corrupt'")
                return super().execute(statement, *args, **kwargs)

        def invalid_connect(*args, **kwargs):
            return original_connect(*args, factory=InvalidIntegrity, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", invalid_connect)
    elif kind == "json":
        read_text = Path.read_text

        def invalid_json(path, *args, **kwargs):
            if path.name == "payload":
                return "{}"
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", invalid_json)
    else:
        verify = workspace.verify

        def invalid_archive(path, **kwargs):
            if kind == "portable" or kwargs.get("password") == PASSWORD:
                raise ValueError("Injected verification failure")
            return verify(path, **kwargs)

        monkeypatch.setattr(workspace, "verify", invalid_archive)
    with pytest.raises((ValueError, RuntimeError), match=r"verification|integrity"):
        create_backup(workspace, kind, target)

    assert not target.exists()
    assert_no_staging(tmp_path)


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_ledger_export_supports_special_source_filenames(workspace, tmp_path, kind):
    renamed = tmp_path / "ledger # source.db"
    workspace.database.backup(renamed)
    workspace.database = LedgerDatabase(renamed)
    target = tmp_path / "backup"

    create_backup(workspace, kind, target)

    assert target.is_file()
    assert renamed.is_file()
    assert_no_staging(tmp_path)


def test_hard_process_abort_leaves_only_recognizable_private_staging(tmp_path):
    target = tmp_path / "backup.aex"
    script = Path(__file__).parents[1] / "fixtures" / "abort_prepared_file.py"

    result = subprocess.run(
        [sys.executable, str(script), str(target)], cwd=tmp_path, capture_output=True, check=False
    )

    assert result.returncode == 23, result.stderr
    assert not target.exists()
    staged = list(tmp_path.glob(".account-eval-write-*/payload"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"not-a-real-pdf"
    if os.name != "nt":
        assert stat.S_IMODE(staged[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(staged[0].parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("kind", FORMATS)
def test_cleanup_failure_keeps_verified_backup_and_protected_staging(
    workspace, tmp_path, monkeypatch, kind
):
    target = tmp_path / "backup"
    rmtree = tempfile.TemporaryDirectory._rmtree

    def fail_final_cleanup(cls, path, *args, **kwargs):
        path = Path(path)
        if (
            path.parent == tmp_path
            and path.name.startswith(".account-eval-write-")
            and target.exists()
        ):
            raise PermissionError("Injected cleanup failure")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", classmethod(fail_final_cleanup))
    with pytest.raises(PermissionError, match="cleanup failure"):
        create_backup(workspace, kind, target)

    staged = list(tmp_path.glob(".account-eval-write-*/payload"))
    assert len(staged) == 1
    assert target.read_bytes() == staged[0].read_bytes()
    if kind == "sqlite":
        with closing(sqlite3.connect(target)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    elif kind == "json":
        assert json.loads(target.read_text())["tables"]["ledger_providers"][0]["id"] == "before"
    else:
        assert (
            workspace.verify(target, password=PASSWORD if kind == "encrypted" else None)["valid"]
            is True
        )
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(staged[0].parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("kind", ["sqlite", "json", "portable"])
def test_cli_explains_existing_backup_target(workspace, tmp_path, capsys, kind):
    target = tmp_path / "existing"
    target.write_bytes(b"changed")
    command = (
        ["portability", "export"]
        if kind == "portable"
        else ["database", "backup" if kind == "sqlite" else "export"]
    )

    result = main(
        [
            "--config",
            str(workspace.project_path),
            "--output",
            "json",
            *command,
            "--output",
            str(target),
        ]
    )

    assert result == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "already_exists"
    assert str(target) in error["message"]
    assert target.read_bytes() == b"changed"

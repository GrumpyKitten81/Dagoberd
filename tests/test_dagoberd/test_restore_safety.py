import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from dagoberd.cli import main
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.services.portability import PortabilityService
from dagoberd.services.restore_files import RestoreError


def workspace(root: Path, content: bytes = b"not-a-real-pdf") -> PortabilityService:
    output = root / "output"
    db = LedgerDatabase(output / "dagoberd.db")
    LedgerRepository(db)
    documents = output / "documents"
    documents.mkdir(parents=True)
    document = documents / "invoice.pdf"
    document.write_bytes(content)
    report = output / "reports" / "cashflow.json"
    report.parent.mkdir()
    report.write_bytes(b"changed" if content == b"changed" else b'{"total": 42}')
    with closing(db.connect()) as connection, connection:
        connection.execute(
            """INSERT INTO ledger_documents(id, filename, storage_path, content_hash)
               VALUES ('document', 'invoice.pdf', ?, ?)""",
            (str(document), hashlib.sha256(content).hexdigest()),
        )
        connection.execute(
            """INSERT INTO ledger_report_runs(id, formats_json, output_paths_json, transaction_count)
               VALUES ('report-run', '["json"]', ?, 0)""",
            (json.dumps([str(report)]),),
        )
    config = root / "dagoberd.toml"
    config.write_text(
        "[database]\npath='output/dagoberd.db'\n"
        "[documents]\nstorage_dir='output/documents'\n"
        "[reporting]\noutput_dir='output'\n",
        encoding="utf-8",
    )
    return PortabilityService(db.path, documents, output, config)


def state(service: PortabilityService) -> tuple[list[str], dict[Path, bytes]]:
    with closing(service.database.connect()) as connection:
        database = list(connection.iterdump())
    files = {
        path: path.read_bytes()
        for root in (service.documents_dir, service.output_dir / "reports")
        for path in root.rglob("*")
        if path.is_file() and not any(p.startswith(".dagoberd-restore-") for p in path.parts)
    }
    return database, files


def rewrite_bundle(bundle: Path, additions: dict[str, bytes]) -> None:
    with zipfile.ZipFile(bundle) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    members.update(additions)
    manifest["entries"] = [
        {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in members.items()
    ]
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, payload in members.items():
            archive.writestr(name, payload)


def assert_clean(service: PortabilityService) -> None:
    assert not list(service.metadata_dir.glob("restore-recovery/restore-*"))
    assert not list(service.documents_dir.rglob(".dagoberd-restore-*"))
    assert not list(service.output_dir.glob("reports/**/.dagoberd-restore-*"))


@pytest.mark.parametrize(
    "additions, message",
    [
        ({"documents/../escape.pdf": b"not-a-real-pdf"}, "Unsafe archive path"),
        ({"documents/C:\\invoice.pdf": b"not-a-real-pdf"}, "Unsafe archive path"),
        ({"documents/./invoice.pdf": b"not-a-real-pdf"}, "same destination"),
        ({"reports/documents/invoice.pdf": b"not-a-real-pdf"}, "same destination"),
        ({"reports/dagoberd.db": b"not-a-real-pdf"}, "protected path"),
        ({"reports/backups/backup.aex": b"not-a-real-pdf"}, "protected path"),
        (
            {
                "documents/nested": b"not-a-real-pdf",
                "documents/nested/invoice.pdf": b"not-a-real-pdf",
            },
            "conflicting",
        ),
    ],
)
def test_restore_rejects_all_invalid_destinations_before_changes(tmp_path, additions, message):
    source = workspace(tmp_path / "source")
    bundle = Path(source.export(tmp_path / "source.aex")["path"])
    rewrite_bundle(bundle, additions)
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)
    database_bytes = target.database.path.read_bytes()

    with pytest.raises(ValueError, match=message):
        target.restore(bundle)

    assert state(target) == before
    assert target.database.path.read_bytes() == database_bytes
    assert not (target.output_dir / "backups").exists()
    assert_clean(target)


@pytest.mark.parametrize("obstacle", ["parent-file", "directory", "unmanaged", "symlink"])
def test_restore_rejects_existing_path_conflicts(tmp_path, monkeypatch, obstacle):
    source = workspace(tmp_path / "source")
    bundle = Path(source.export(tmp_path / "source.aex")["path"])
    target = workspace(tmp_path / "target", b"changed")
    if obstacle == "parent-file":
        (target.documents_dir / "nested").write_bytes(b"changed")
        rewrite_bundle(bundle, {"documents/nested/invoice.pdf": b"not-a-real-pdf"})
    elif obstacle == "directory":
        (target.documents_dir / "nested").mkdir()
        rewrite_bundle(bundle, {"documents/nested": b"not-a-real-pdf"})
    elif obstacle == "unmanaged":
        (target.output_dir / "reports" / "other.json").write_bytes(b"changed")
        rewrite_bundle(bundle, {"reports/reports/other.json": b'{"total": 42}'})
    else:
        destination = target.documents_dir / "invoice.pdf"
        is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path == destination or is_symlink(path)
        )
    before = state(target)

    with pytest.raises(ValueError, match=r"parent|regular file|unmanaged|symbolic links"):
        target.restore(bundle)

    assert state(target) == before
    assert_clean(target)


@pytest.mark.parametrize("failure", ["write", "checksum"])
def test_restore_prepares_every_file_before_replacing_database(tmp_path, monkeypatch, failure):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)
    original = Path.write_bytes
    writes = 0

    def fail_staging(path, payload):
        nonlocal writes
        if path.name.startswith("incoming-"):
            writes += 1
            if writes == 2:
                if failure == "checksum":
                    return original(path, b"changed")
                raise OSError("Disk full during preparation")
        return original(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_staging)
    with pytest.raises((OSError, ValueError), match=r"Disk full|checksum"):
        target.restore(bundle)

    assert writes == 2
    assert state(target) == before
    assert_clean(target)


def test_restore_refuses_to_continue_without_rollback_export(tmp_path, monkeypatch):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)

    def fail_export(path):
        raise OSError("Cannot create rollback export")

    monkeypatch.setattr(target, "export", fail_export)
    with pytest.raises(OSError, match="rollback export"):
        target.restore(bundle)

    assert state(target) == before
    assert_clean(target)


@pytest.mark.parametrize("invalid_database", ["foreign-key", "report-paths"])
def test_restore_validates_prepared_database_before_changes(tmp_path, invalid_database):
    source = workspace(tmp_path / "source")
    bundle = Path(source.export(tmp_path / "source.aex")["path"])
    database = tmp_path / "invalid.sqlite3"
    with zipfile.ZipFile(bundle) as archive:
        database.write_bytes(archive.read("ledger/ledger.sqlite3"))
    with closing(sqlite3.connect(database)) as connection, connection:
        if invalid_database == "foreign-key":
            connection.execute(
                """INSERT INTO ledger_document_origins(
                       id, document_id, origin_key, relative_path, original_filename,
                       content_hash, mime_type, size_bytes
                   ) VALUES ('origin', 'missing-document', 'origin', 'invoice.pdf', 'invoice.pdf',
                             'hash', 'application/pdf', 1)"""
            )
        else:
            connection.execute("UPDATE ledger_report_runs SET output_paths_json='not json'")
    rewrite_bundle(bundle, {"ledger/ledger.sqlite3": database.read_bytes()})
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)

    with pytest.raises(ValueError):
        target.restore(bundle)

    assert state(target) == before
    assert not (target.output_dir / "backups").exists()
    assert_clean(target)


def test_restore_rejects_duplicate_manifest_entries(tmp_path):
    source = workspace(tmp_path / "source")
    bundle = Path(source.export(tmp_path / "source.aex")["path"])
    with zipfile.ZipFile(bundle) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest["entries"].append(manifest["entries"][0])
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)

    with pytest.raises(ValueError, match="duplicate entries"):
        target.restore(bundle)

    assert state(target) == before
    assert_clean(target)


@pytest.mark.parametrize(
    "phase", ["database", "document", "report", "delete", "post-check", "corrupt-file", "interrupt"]
)
def test_restore_rolls_back_database_files_and_new_paths(tmp_path, monkeypatch, phase):
    source = workspace(tmp_path / "source")
    new_document = source.documents_dir / "new.pdf"
    new_document.write_bytes(b"not-a-real-pdf")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    obsolete = target.documents_dir / "obsolete.pdf"
    obsolete.write_bytes(b"changed")
    before = state(target)
    replace = os.replace
    restore = target.database.restore
    unlink = Path.unlink
    integrity = target.database.integrity_check
    failed = False

    def fail_once():
        nonlocal failed
        failed = True
        raise OSError("Injected restore failure")

    def replace_file(src, dst):
        if not failed and Path(src).name.startswith("incoming-"):
            if phase == "document" and Path(dst).name == "invoice.pdf":
                fail_once()
            if phase == "report" and Path(dst).suffix == ".json":
                fail_once()
            if phase == "interrupt":
                raise KeyboardInterrupt
        result = replace(src, dst)
        if (
            phase == "corrupt-file"
            and Path(src).name.startswith("incoming-")
            and Path(dst).name == "invoice.pdf"
        ):
            Path(dst).write_bytes(b"changed")
        return result

    def restore_database(path):
        restore(path)
        if phase == "database" and not failed:
            fail_once()

    def unlink_file(path, *args, **kwargs):
        if phase == "delete" and path == obsolete and not failed:
            fail_once()
        return unlink(path, *args, **kwargs)

    def check_integrity():
        nonlocal failed
        if phase == "post-check" and not failed:
            failed = True
            return {"valid": False}
        return integrity()

    monkeypatch.setattr(os, "replace", replace_file)
    monkeypatch.setattr(target.database, "restore", restore_database)
    monkeypatch.setattr(Path, "unlink", unlink_file)
    monkeypatch.setattr(target.database, "integrity_check", check_integrity)
    with pytest.raises(RestoreError, match="previous state restored") as caught:
        target.restore(bundle)

    assert caught.value.rollback_complete is True
    assert caught.value.details["rollback_failures"] == []
    assert Path(caught.value.details["rollback_export"]).is_file()
    assert state(target) == before
    assert not (target.documents_dir / "new.pdf").exists()
    assert target.integrity()["valid"] is True
    assert_clean(target)


@pytest.mark.parametrize("failure", ["file", "database"])
def test_failed_rollback_retains_recovery_material_and_blocks_retry(tmp_path, monkeypatch, failure):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    replace = os.replace
    report = target.output_dir / "reports" / "cashflow.json"

    def fail_file(src, dst):
        if Path(dst) == report:
            raise PermissionError("Injected persistent file failure")
        return replace(src, dst)

    def fail_database(path):
        raise sqlite3.OperationalError("Injected persistent database failure")

    if failure == "file":
        monkeypatch.setattr(os, "replace", fail_file)
    else:
        monkeypatch.setattr(target.database, "restore", fail_database)
    with pytest.raises(RestoreError, match="manual recovery required") as caught:
        target.restore(bundle)

    error = caught.value
    assert error.rollback_complete is False
    assert error.details["rollback_failures"]
    recovery = Path(error.details["recovery_directory"])
    record = json.loads((recovery / "recovery.json").read_text())
    assert Path(record["previous_database"]).is_file()
    assert target.verify(record["rollback_export"])["valid"] is True
    if failure == "file":
        original = next(item for item in record["files"] if item["destination"] == str(report))
        assert Path(original["previous"]).read_bytes() == b"changed"
    with pytest.raises(RestoreError, match="Unresolved restore recovery"):
        target.restore(bundle)
    recovered = workspace(tmp_path / "recovered")
    recovered.restore(record["rollback_export"])
    assert recovered.integrity()["valid"] is True
    assert (recovered.documents_dir / "invoice.pdf").read_bytes() == b"changed"
    assert (recovered.output_dir / "reports/cashflow.json").read_bytes() == b"changed"


def test_successful_restore_rebases_documents_and_keeps_unregistered_reports(tmp_path):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    unrelated = target.output_dir / "reports" / "other.json"
    unrelated.write_bytes(b"changed")
    obsolete = target.documents_dir / "obsolete.pdf"
    obsolete.write_bytes(b"changed")

    result = target.restore(bundle)

    assert result["restored_files"] == 2
    assert (target.documents_dir / "invoice.pdf").read_bytes() == b"not-a-real-pdf"
    assert unrelated.read_bytes() == b"changed"
    assert not obsolete.exists()
    assert target.integrity()["valid"] is True
    assert_clean(target)


@pytest.mark.parametrize("existing_document", [False, True])
def test_restore_into_empty_workspace_removes_new_database_on_failure(
    tmp_path, monkeypatch, existing_document
):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    root = tmp_path / "empty"
    target = PortabilityService(
        root / "output/ledger.db", root / "documents", root / "output", root / "dagoberd.toml"
    )
    if existing_document:
        target.documents_dir.mkdir(parents=True)
        (target.documents_dir / "invoice.pdf").write_bytes(b"changed")
    replace = os.replace
    failed = False

    def fail_replace(src, dst):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Injected restore failure")
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RestoreError, match="previous state restored"):
        target.restore(bundle)

    assert not target.database.path.exists()
    if existing_document:
        assert (target.documents_dir / "invoice.pdf").read_bytes() == b"changed"
    else:
        assert not list(target.documents_dir.rglob("*.pdf"))
    assert_clean(target)


def test_restore_uses_one_verified_source_snapshot(tmp_path, monkeypatch):
    source = workspace(tmp_path / "source")
    bundle = Path(source.export(tmp_path / "source.aex")["path"])
    target = workspace(tmp_path / "target", b"changed")
    verify = target.verify

    def replace_source_after_verification(path, **kwargs):
        result = verify(path, **kwargs)
        if Path(path).name == "source":
            bundle.write_bytes(b"changed")
        return result

    monkeypatch.setattr(target, "verify", replace_source_after_verification)
    assert target.restore(bundle)["restored"] is True
    assert target.integrity()["valid"] is True


@pytest.mark.parametrize("transfer_fails", [False, True])
def test_cleanup_failure_keeps_record_and_reports_actual_data_state(
    tmp_path, monkeypatch, transfer_fails
):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    before = state(target)
    rmtree = shutil.rmtree
    replace = os.replace
    failed = False

    def fail_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".dagoberd-restore-"):
            raise PermissionError("Cannot remove staging directory")
        return rmtree(path, *args, **kwargs)

    def fail_transfer_once(src, dst):
        nonlocal failed
        if transfer_fails and not failed:
            failed = True
            raise OSError("Injected transfer failure")
        return replace(src, dst)

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    monkeypatch.setattr(os, "replace", fail_transfer_once)
    with pytest.raises(RestoreError, match="cleanup") as caught:
        target.restore(bundle)

    error = caught.value
    assert error.restore_complete is not transfer_fails
    assert error.rollback_complete is transfer_fails
    assert error.details["cleanup_failures"]
    assert error.details["rollback_failures"] == []
    assert (Path(error.details["recovery_directory"]) / "recovery.json").is_file()
    assert target.integrity()["valid"] is True
    if transfer_fails:
        assert state(target) == before
    else:
        assert (target.documents_dir / "invoice.pdf").read_bytes() == b"not-a-real-pdf"


def test_cli_rejects_invalid_restore_without_initializing_ledger(tmp_path, capsys):
    config = tmp_path / "dagoberd.toml"
    config.write_text("[database]\npath='ledger.db'\n", encoding="utf-8")
    invalid = tmp_path / "invalid.aex"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("file", b"changed")

    assert (
        main(
            [
                "--config",
                str(config),
                "--output",
                "json",
                "portability",
                "restore",
                str(invalid),
                "--yes",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"
    assert not (tmp_path / "ledger.db").exists()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_cli_reports_rollback_status_and_recovery_paths(
    tmp_path, monkeypatch, capsys, rollback_fails
):
    source = workspace(tmp_path / "source")
    bundle = source.export(tmp_path / "source.aex")["path"]
    target = workspace(tmp_path / "target", b"changed")
    replace = os.replace
    failed = False

    def fail_replace(src, dst):
        nonlocal failed
        if not failed or rollback_fails:
            failed = True
            raise OSError("private failure text")
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    code = main(
        [
            "--config",
            str(target.project_path),
            "--output",
            "json",
            "portability",
            "restore",
            str(bundle),
            "--yes",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == (7 if rollback_fails else 1)
    assert payload["ok"] is False
    assert payload["error"]["code"] == (
        "restore_recovery_required" if rollback_fails else "restore_rolled_back"
    )
    assert payload["error"]["details"]["rollback_complete"] is not rollback_fails
    assert Path(payload["error"]["details"]["rollback_export"]).is_file()
    assert "private failure text" not in json.dumps(payload)

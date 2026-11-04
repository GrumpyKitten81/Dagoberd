import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from account_eval import __version__
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.migrations import CURRENT_SCHEMA_VERSION
from account_eval.services.portability import EXPORT_FORMAT, PortabilityService

SOURCES = [
    (PurePosixPath, "/source/output", "/source/documents"),
    (PureWindowsPath, r"C:\Source\output", r"C:\Source\documents"),
    (PureWindowsPath, "C:/Source/output", "C:/Source/documents"),
    (PureWindowsPath, r"\\server\share\output", r"\\server\share\documents"),
    (PureWindowsPath, "//server/share/output", "//server/share/documents"),
]


def write_archive(path, manifest, members):
    kinds = {
        "ledger": "ledger",
        "documents": "document",
        "reports": "report",
        "metadata": "configuration",
    }
    manifest["entries"] = [
        {
            "path": name,
            "kind": kinds[PurePosixPath(name).parts[0]],
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in members.items()
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, payload in members.items():
            archive.writestr(name, payload)


def source_archive(
    tmp_path, flavor=PureWindowsPath, output=r"C:\Source\output", documents=r"C:\Source\documents"
):
    database = LedgerDatabase(tmp_path / "source.db")
    database.initialize()
    with closing(database.connect()) as connection, connection:
        connection.execute(
            """INSERT INTO ledger_documents(id, filename, storage_path, content_hash)
               VALUES ('document', 'invoice.pdf', ?, ?)""",
            (str(flavor(documents) / "invoice.pdf"), hashlib.sha256(b"not-a-real-pdf").hexdigest()),
        )
        connection.execute(
            "INSERT INTO ledger_documents(id, filename) VALUES ('metadata', 'metadata.pdf')"
        )
        connection.execute(
            """INSERT INTO ledger_report_runs(id, formats_json, output_paths_json, transaction_count)
               VALUES ('report', '["json"]', ?, 0)""",
            (json.dumps([str(flavor(output) / "reports/cashflow.json")]),),
        )
        connection.execute(
            """INSERT INTO ledger_analysis_runs(
                   id, definition_version, definition_json, result_json, result_hash, output_paths_json
               ) VALUES ('analysis', 1, '{}', '{}', 'hash', ?)""",
            (json.dumps([str(flavor(output) / "analyses/cashflow.json")]),),
        )
    members = {
        "ledger/ledger.sqlite3": database.path.read_bytes(),
        "metadata/config.json": b"{}",
        "documents/invoice.pdf": b"not-a-real-pdf",
        "reports/reports/cashflow.json": b'{"total": 42}',
        "reports/analyses/cashflow.json": b'{"total": 42}',
    }
    manifest = {
        "format": EXPORT_FORMAT,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "product_version": __version__,
        "source_roots": {"output": output, "documents": documents},
    }
    path = tmp_path / "source.aex"
    write_archive(path, manifest, members)
    return path


def mutate_archive(tmp_path, path, mutate):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        members = {
            name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
        }
    database = tmp_path / "modified.sqlite3"
    database.write_bytes(members["ledger/ledger.sqlite3"])
    with closing(sqlite3.connect(database)) as connection, connection:
        mutate(manifest, members, connection)
    members["ledger/ledger.sqlite3"] = database.read_bytes()
    write_archive(path, manifest, members)


def target_workspace(tmp_path):
    root = tmp_path / "target"
    database = LedgerDatabase(root / "ledger.db")
    database.initialize()
    with closing(database.connect()) as connection, connection:
        connection.execute(
            "INSERT INTO ledger_providers(id, kind, display_name) VALUES ('before', 'test', 'Before')"
        )
    documents = root / "documents"
    documents.mkdir()
    (documents / "old.pdf").write_bytes(b"changed")
    return PortabilityService(database.path, documents, root / "output", root / "account-eval.toml")


def assert_references(service):
    document = service.documents_dir / "invoice.pdf"
    report = service.output_dir / "reports/cashflow.json"
    analysis = service.output_dir / "analyses/cashflow.json"
    assert document.read_bytes() == b"not-a-real-pdf"
    assert report.read_bytes() == b'{"total": 42}'
    assert analysis.read_bytes() == b'{"total": 42}'
    with closing(service.database.connect()) as connection:
        assert connection.execute(
            "SELECT storage_path FROM ledger_documents WHERE id='document'"
        ).fetchone()[0] == str(document)
        assert (
            connection.execute(
                "SELECT storage_path FROM ledger_documents WHERE id='metadata'"
            ).fetchone()[0]
            is None
        )
        assert json.loads(
            connection.execute(
                "SELECT output_paths_json FROM ledger_report_runs WHERE id='report'"
            ).fetchone()[0]
        ) == [str(report)]
        assert json.loads(
            connection.execute(
                "SELECT output_paths_json FROM ledger_analysis_runs WHERE id='analysis'"
            ).fetchone()[0]
        ) == [str(analysis)]
    assert service.integrity()["valid"] is True


@pytest.mark.parametrize("flavor, output, documents", SOURCES)
def test_restore_rebases_source_paths_without_host_path_assumptions(
    tmp_path, flavor, output, documents
):
    archive = source_archive(tmp_path, flavor, output, documents)
    target = target_workspace(tmp_path)

    result = target.restore(archive)

    assert result["restored_files"] == 3
    assert_references(target)
    assert not (target.documents_dir / "old.pdf").exists()


@pytest.mark.parametrize("flavor, output, documents", SOURCES[1:])
def test_windows_references_match_case_and_separator_variants(tmp_path, flavor, output, documents):
    archive = source_archive(tmp_path, flavor, output, documents)
    target = target_workspace(tmp_path)

    def change_case(manifest, members, connection):
        connection.execute(
            "UPDATE ledger_documents SET storage_path=upper(replace(storage_path, '\\', '/')) WHERE storage_path IS NOT NULL"
        )
        for table in ("ledger_report_runs", "ledger_analysis_runs"):
            row = connection.execute(f"SELECT output_paths_json FROM {table}").fetchone()
            paths = [path.upper().replace("\\", "/") for path in json.loads(row[0])]
            connection.execute(f"UPDATE {table} SET output_paths_json=?", (json.dumps(paths),))

    mutate_archive(tmp_path, archive, change_case)
    target.restore(archive)

    assert_references(target)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-document",
        "missing-report",
        "missing-analysis",
        "outside-document-root",
        "outside-report-root",
        "relative-reference",
        "parent-traversal",
        "missing-root",
        "relative-root",
        "invalid-root-type",
        "invalid-roots-object",
        "invalid-report-list",
        "invalid-report-value",
        "ambiguous-case",
    ],
)
def test_unresolvable_references_fail_before_target_changes(tmp_path, problem):
    archive = source_archive(tmp_path)
    target = target_workspace(tmp_path)
    before_database = target.database.path.read_bytes()
    before_documents = {
        path: path.read_bytes() for path in target.documents_dir.rglob("*") if path.is_file()
    }

    def invalidate(manifest, members, connection):
        if problem == "missing-document":
            del members["documents/invoice.pdf"]
        elif problem == "missing-report":
            del members["reports/reports/cashflow.json"]
        elif problem == "missing-analysis":
            del members["reports/analyses/cashflow.json"]
        elif problem == "outside-document-root":
            connection.execute(
                "UPDATE ledger_documents SET storage_path='D:\\Other\\invoice.pdf' WHERE id='document'"
            )
        elif problem == "outside-report-root":
            connection.execute(
                "UPDATE ledger_report_runs SET output_paths_json=?",
                (json.dumps([r"C:\Elsewhere\cashflow.json"]),),
            )
        elif problem == "relative-reference":
            connection.execute(
                "UPDATE ledger_documents SET storage_path='invoice.pdf' WHERE id='document'"
            )
        elif problem == "parent-traversal":
            connection.execute(
                "UPDATE ledger_documents SET storage_path='C:\\Source\\documents\\..\\documents\\invoice.pdf' WHERE id='document'"
            )
        elif problem == "missing-root":
            del manifest["source_roots"]["documents"]
        elif problem == "relative-root":
            manifest["source_roots"]["documents"] = "documents"
        elif problem == "invalid-root-type":
            manifest["source_roots"]["documents"] = 42
        elif problem == "invalid-roots-object":
            manifest["source_roots"] = []
        elif problem == "invalid-report-list":
            connection.execute("UPDATE ledger_report_runs SET output_paths_json='{}'")
        elif problem == "invalid-report-value":
            connection.execute("UPDATE ledger_report_runs SET output_paths_json='[42]'")
        else:
            members["documents/INVOICE.PDF"] = b"changed"

    mutate_archive(tmp_path, archive, invalidate)

    with pytest.raises(ValueError, match=r"Restore|same destination"):
        target.restore(archive)

    assert target.database.path.read_bytes() == before_database
    assert {
        path: path.read_bytes() for path in target.documents_dir.rglob("*") if path.is_file()
    } == before_documents
    assert not target.output_dir.exists()
    assert not target.metadata_dir.exists()


def test_posix_references_do_not_use_windows_case_matching(tmp_path):
    archive = source_archive(tmp_path, PurePosixPath, "/source/output", "/source/documents")
    target = target_workspace(tmp_path)

    def change_case(manifest, members, connection):
        connection.execute(
            "UPDATE ledger_documents SET storage_path='/source/documents/INVOICE.PDF' WHERE id='document'"
        )

    mutate_archive(tmp_path, archive, change_case)
    with pytest.raises(ValueError, match="no matching archived file"):
        target.restore(archive)

    assert (target.documents_dir / "old.pdf").read_bytes() == b"changed"

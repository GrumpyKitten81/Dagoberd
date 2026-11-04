import errno
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dagoberd.cli import main
from dagoberd.config import ComdirectConnectionConfig
from dagoberd.importers.camt053 import Camt053Importer
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.mcp_server import MCPService
from dagoberd.providers.comdirect import ComdirectProvider, ComdirectSessionState
from dagoberd.services.comdirect_diagnostics import ComdirectDiagnosticsService
from dagoberd.services.import_service import ImportService
from dagoberd.services.portability import PortabilityService

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt><Stmt><Id>statement-1</Id>
    <Acct><Id><IBAN>ES1234567890</IBAN></Id><Ccy>EUR</Ccy></Acct>
    <Ntry><NtryRef>entry-1</NtryRef><Amt Ccy="EUR">125.50</Amt>
      <CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
      <BookgDt><Dt>2026-07-01</Dt></BookgDt>
      <ValDt><Dt>2026-07-02</Dt></ValDt>
      <NtryDtls><TxDtls><Refs><EndToEndId>invoice-1</EndToEndId></Refs>
        <RltdPties><Dbtr><Nm>Tenant</Nm></Dbtr></RltdPties>
        <RmtInf><Ustrd>Rent July</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
    <Ntry><AcctSvcrRef>entry-2</AcctSvcrRef><Amt Ccy="EUR">20.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>PDNG</Cd></Sts>
      <BookgDt><Dt>2026-07-03</Dt></BookgDt>
      <AddtlNtryInf>Fee</AddtlNtryInf>
    </Ntry>
  </Stmt></BkToCstmrStmt>
</Document>"""


def repository(tmp_path: Path) -> LedgerRepository:
    return LedgerRepository(LedgerDatabase(tmp_path / "account-eval.db"))


def test_camt053_import_is_account_specific_signed_and_idempotent(tmp_path):
    repo = repository(tmp_path)
    path = tmp_path / "statement.xml"
    path.write_text(CAMT, encoding="utf-8")
    service = ImportService(repo)

    preview = service.preview("camt053", path)
    first = service.execute(preview.preview_id)
    duplicate = service.preview("camt053", path)
    second = service.execute(duplicate.preview_id)

    transactions = repo.list_transactions()
    assert preview.errors == ()
    assert preview.count == 2
    assert {item["amount_minor"] for item in transactions} == {12550, -2000}
    assert {item["account_external_id"] for item in transactions} == {"ES1234567890"}
    assert {item["booking_status"] for item in transactions} == {"booked", "pending"}
    assert (first.imported, first.skipped) == (2, 0)
    assert duplicate.duplicate_candidates == 2
    assert (second.imported, second.skipped) == (0, 2)


def test_camt_rejects_entities_without_parsing(tmp_path):
    path = tmp_path / "unsafe.xml"
    path.write_text("<!DOCTYPE x [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><x>&xxe;</x>")

    batch = Camt053Importer().inspect_transactions(path)

    assert batch.issues[0].code == "unsafe_xml"


def test_tabular_profile_returns_all_structured_errors_and_blocks_execution(tmp_path):
    repo = repository(tmp_path)
    path = tmp_path / "broken.csv"
    path.write_text("when;what;money\ninvalid;;oops\n2026-07-01;Good;12,34\n", encoding="utf-8")
    profiles = {
        "custom": {
            "source": "tabular",
            "mapping": {
                "booking_date": ("when",),
                "purpose": ("what",),
                "amount": ("money",),
            },
            "delimiter": ";",
            "encoding": "utf-8-sig",
            "sheet_name": 0,
            "default_currency": "EUR",
            "date_formats": ("%Y-%m-%d",),
            "decimal_separator": ",",
        }
    }
    service = ImportService(repo, profiles=profiles)

    preview = service.preview("tabular", path, profile="custom")

    assert preview.rows_total == 2
    assert preview.count == 1
    assert preview.errors[0]["row"] == 1
    assert preview.errors[0]["code"] == "required"
    with pytest.raises(ValueError, match="validation errors"):
        service.execute(preview.preview_id)
    assert repo.list_transactions() == []


def portability(tmp_path: Path) -> PortabilityService:
    db = LedgerDatabase(tmp_path / "output" / "account-eval.db")
    LedgerRepository(db)
    documents = tmp_path / "output" / "documents"
    documents.mkdir(parents=True)
    (documents / "invoice.pdf").write_bytes(b"not-a-real-pdf")
    return PortabilityService(
        db.path,
        documents,
        tmp_path / "output",
        tmp_path / "account-eval.toml",
        masked_config={"database": {"path": str(db.path)}},
    )


def test_portable_export_encrypted_backup_verify_and_restore(tmp_path):
    service = portability(tmp_path)
    exported = service.export(tmp_path / "portable.aex")
    encrypted = service.encrypted_backup(
        tmp_path / "portable.aebak",
        "correct horse battery staple",
    )

    assert exported["valid"] is True
    assert encrypted["valid"] is True
    assert service.verify(tmp_path / "portable.aex")["entries"] >= 3
    with pytest.raises(ValueError, match=r"password|authentication"):
        service.verify(tmp_path / "portable.aebak", password="wrong password value")

    with service.database.connect() as connection:
        connection.execute("DELETE FROM ledger_providers")
    restored = service.restore(
        tmp_path / "portable.aebak",
        password="correct horse battery staple",
    )
    assert restored["restored"] is True
    assert Path(restored["rollback_export"]).is_file()
    assert service.integrity()["valid"] is True


def test_portable_restore_rebases_registered_reports_to_new_workspace(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source = portability(source_root)
    report = source_root / "output" / "reports" / "cashflow.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"total": 42}', encoding="utf-8")
    with source.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_report_runs(
                   id, formats_json, output_paths_json, transaction_count
               ) VALUES ('report-run', '[\"json\"]', ?, 0)""",
            (json.dumps([str(report)]),),
        )
    bundle = source.export(tmp_path / "portable-workspace.aex")

    target_root = tmp_path / "target"
    target = portability(target_root)
    restored_report = target_root / "output" / "reports" / "cashflow.json"
    restored_document = target_root / "output" / "documents" / "invoice.pdf"
    expected_destinations = {restored_document, restored_report}
    replaced_destinations: set[Path] = set()
    replace = os.replace

    def replace_on_same_filesystem(source: str | Path, destination: str | Path) -> None:
        destination_path = Path(destination)
        if destination_path in expected_destinations:
            if Path(source).parent.parent != destination_path.parent:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            replaced_destinations.add(destination_path)
        replace(source, destination)

    monkeypatch.setattr("dagoberd.services.portability.os.replace", replace_on_same_filesystem)
    target.restore(bundle["path"])

    assert replaced_destinations == expected_destinations
    assert restored_document.read_bytes() == b"not-a-real-pdf"
    assert restored_report.read_text(encoding="utf-8") == '{"total": 42}'
    assert target.integrity()["valid"] is True
    with target.database.connect() as connection:
        paths = json.loads(
            connection.execute(
                "SELECT output_paths_json FROM ledger_report_runs WHERE id='report-run'"
            ).fetchone()[0]
        )
    assert paths == [str(restored_report)]


def test_deletion_requires_unchanged_fingerprint_and_writes_receipt(tmp_path):
    service = portability(tmp_path)
    preview = service.preview_deletion(["documents"])
    document = tmp_path / "output" / "documents" / "invoice.pdf"
    document.write_bytes(b"changed")

    with pytest.raises(ValueError, match="changed after preview"):
        service.execute_deletion(preview.preview_id, preview.fingerprint)

    preview = service.preview_deletion(["documents"])
    result = service.execute_deletion(preview.preview_id, preview.fingerprint)
    assert result["deleted"] == 1
    assert Path(result["receipt"]).is_file()
    assert not document.exists()


def test_integrity_reports_missing_registered_documents(tmp_path):
    service = portability(tmp_path)
    missing = tmp_path / "output" / "documents" / "missing.pdf"
    with service.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_documents(
                   id, filename, storage_path, content_hash
               ) VALUES ('missing-document', 'missing.pdf', ?, 'abcd')""",
            (str(missing),),
        )

    result = service.integrity()

    assert result["valid"] is False
    assert result["documents"]["missing"] == ["missing-document"]


def test_portability_cli_export_verify_integrity_and_deletion_preview(
    tmp_path,
    capsys,
):
    config = tmp_path / "account-eval.toml"
    config.write_text(
        "[database]\npath='output/account-eval.db'\n"
        "[reporting]\noutput_dir='output'\n"
        "[documents]\nstorage_dir='output/documents'\n"
        "[comdirect.connections.private]\nenabled=false\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "doctor"]) == 0
    capsys.readouterr()
    export = tmp_path / "output" / "portable.aex"
    assert (
        main(
            [
                "--config",
                str(config),
                "portability",
                "export",
                "--output",
                str(export),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config),
                "portability",
                "verify",
                str(export),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert (
        main(
            [
                "--config",
                str(config),
                "portability",
                "integrity",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config),
                "portability",
                "delete-preview",
                "--scope",
                "exports",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["targets"][0]["path"] == str(export)


def test_phase4_mcp_contract_uses_idempotency_and_secret_references(tmp_path):
    config = tmp_path / "account-eval.toml"
    config.write_text(
        "[database]\npath='output/account-eval.db'\n"
        "[reporting]\noutput_dir='output'\n"
        "[documents]\nstorage_dir='output/documents'\n"
        "[comdirect.connections.private]\nenabled=false\n",
        encoding="utf-8",
    )
    service = MCPService(config_path=str(config))
    service.secrets = MemorySecrets(
        {
            "BACKUP_KEY": "correct horse battery staple",
        }
    )
    exported_path = tmp_path / "output" / "mcp-export.aex"
    arguments = {
        "output": str(exported_path),
        "idempotency_key": "phase4-export-0001",
    }

    first = service.execute("export_portable_data", arguments)
    repeated = service.execute("export_portable_data", arguments)
    encrypted = service.execute(
        "create_encrypted_backup",
        {
            "output": str(tmp_path / "output" / "mcp-backup.aebak"),
            "encryption_secret": "BACKUP_KEY",
            "idempotency_key": "phase4-backup-0001",
        },
    )

    assert first == repeated
    assert first["valid"] is True
    assert encrypted["encrypted"] is True
    assert service.execute("check_data_integrity", {})["valid"] is True
    assert service.execute("diagnose_comdirect", {"connection": "private"})["read_only"] is True


def test_mcp_comdirect_sync_requires_explicit_local_opt_in(tmp_path):
    config = tmp_path / "account-eval.toml"
    config.write_text(
        "[database]\npath='output/account-eval.db'\n"
        "[comdirect.connections.private]\n"
        "enabled=true\nsession_mode='keychain_timed'\nallow_mcp_sync=false\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not locally enabled"):
        MCPService(config_path=str(config)).execute(
            "sync_comdirect",
            {"connection": "private", "idempotency_key": "mcp-comdirect-0001"},
        )


class MemorySecrets:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)

    def get_keyring(self, name):
        return self.values.get(name)

    def delete(self, name):
        self.values.pop(name, None)
        return True


def test_mcp_expired_session_is_revoked_and_removed_per_connection(tmp_path, monkeypatch):
    config = tmp_path / "account-eval.toml"
    config.write_text(
        """[comdirect.connections.private]
enabled = true
session_mode = "keychain_timed"
allow_mcp_sync = true
""",
        encoding="utf-8",
    )
    state = ComdirectSessionState(
        "expired-access",
        "session",
        None,
        None,
        (datetime.now(UTC) - timedelta(hours=13)).isoformat(),
    )
    service = MCPService(config_path=str(config))
    service.secrets = MemorySecrets({"comdirect.private.timed-session": state.to_json()})
    revoked: list[str] = []
    monkeypatch.setattr(
        ComdirectProvider,
        "revoke",
        lambda self: revoked.append("private"),
    )

    with pytest.raises(ValueError, match="abgelaufen"):
        service.execute(
            "sync_comdirect",
            {"connection": "private", "idempotency_key": "mcp-comdirect-0001"},
        )

    assert revoked == ["private"]
    assert service.secrets.get_keyring("comdirect.private.timed-session") is None


def test_comdirect_diagnostics_is_sanitized_and_actionable(tmp_path):
    repo = repository(tmp_path)
    config = ComdirectConnectionConfig(
        enabled=True,
        username_secret="comdirect.private.username",
        client_id_secret="comdirect.private.client-id",
        client_secret_secret="comdirect.private.client-secret",
    )
    state = ComdirectSessionState(
        "secret-access",
        "session",
        "secret-refresh",
        (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        datetime.now(UTC).isoformat(),
    )
    secrets = MemorySecrets(
        {
            config.username_secret: "secret-user",
            config.client_id_secret: "secret-client",
            config.client_secret_secret: "secret-client-password",
            "comdirect.private.timed-session": state.to_json(),
        }
    )

    result = ComdirectDiagnosticsService(repo, "private", config, secrets).diagnose()

    rendered = json.dumps(result)
    assert result["session"]["status"] == "not_used"
    assert result["next_action"] == "sync"
    assert "secret-access" not in rendered
    assert "secret-client-password" not in rendered

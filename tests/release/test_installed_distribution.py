from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import dagoberd
from dagoberd import __version__
from dagoberd.accounts import AccountScope
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.services.import_service import ImportService
from dagoberd.services.portability import PortabilityService

CAMT_STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt><Stmt><Id>release-statement</Id>
    <Acct><Id><IBAN>ES1234567890</IBAN></Id><Ccy>EUR</Ccy></Acct>
    <Ntry><NtryRef>release-entry</NtryRef><Amt Ccy="EUR">125.50</Amt>
      <CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
      <BookgDt><Dt>2026-07-01</Dt></BookgDt>
      <NtryDtls><TxDtls>
        <Refs><EndToEndId>release-invoice</EndToEndId></Refs>
        <RltdPties><Dbtr><Nm>Release Tenant</Nm></Dbtr></RltdPties>
        <RmtInf><Ustrd>Release rent</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
  </Stmt></BkToCstmrStmt>
</Document>"""


def console_script(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    executable_name = f"{name}{suffix}"
    python_dir = Path(sys.executable).parent
    candidates = (
        python_dir / executable_name,
        python_dir / "Scripts" / executable_name,
        Path(found) if (found := shutil.which(name)) else None,
    )
    script = next(
        (candidate for candidate in candidates if candidate and candidate.is_file()), None
    )
    if script is None:
        raise AssertionError(f"Console script is missing: {name}")
    return script


def test_distribution_metadata_and_console_scripts() -> None:
    assert version("dagoberd") == __version__
    assert console_script("dagoberd").is_file()
    assert console_script("dagoberd-mcp").is_file()
    if os.environ.get("DAGOBERD_RELEASE_ARTIFACT") == "1":
        module_path = Path(dagoberd.__file__).resolve()
        assert "site-packages" in module_path.parts or "dist-packages" in module_path.parts


def test_cli_initializes_and_diagnoses_local_workspace(tmp_path: Path) -> None:
    initialized = subprocess.run(
        [str(console_script("dagoberd")), "init"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert (tmp_path / "dagoberd.toml").is_file()

    diagnosed = subprocess.run(
        [str(console_script("dagoberd")), "doctor"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert diagnosed.returncode == 0, diagnosed.stderr
    diagnostics = json.loads(diagnosed.stdout)
    assert any(item["name"] == "database" for item in diagnostics)


def test_account_scoped_import_report_and_encrypted_backup(tmp_path: Path) -> None:
    database = LedgerDatabase(tmp_path / "output" / "dagoberd.db")
    repository = LedgerRepository(database)
    statement = tmp_path / "statement.xml"
    statement.write_text(CAMT_STATEMENT, encoding="utf-8")

    imports = ImportService(repository)
    preview = imports.preview("camt053", statement)
    execution = imports.execute(preview.preview_id)
    transaction = repository.list_transactions()[0]
    account_id = transaction["account_id"]

    assert execution.imported == 1
    assert transaction["account_external_id"] == "ES1234567890"
    report_paths = LedgerReportGenerator(repository, tmp_path / "output" / "reports").generate(
        formats=("json",),
        account_scope=AccountScope.selected([account_id]),
    )
    report = json.loads(report_paths[0].read_text(encoding="utf-8"))
    assert report["metadata"]["account_scope"] == {
        "mode": "selected",
        "account_ids": [account_id],
    }
    assert report["metadata"]["transaction_count"] == 1

    project_path = tmp_path / "dagoberd.toml"
    project_path.write_text("[database]\npath='output/dagoberd.db'\n", encoding="utf-8")
    portability = PortabilityService(
        database.path,
        tmp_path / "output" / "documents",
        tmp_path / "output",
        project_path,
        masked_config={"database": {"path": str(database.path)}},
    )
    backup = tmp_path / "backup.aebak"
    created = portability.encrypted_backup(backup, "release-test-password")
    verified = portability.verify(backup, password="release-test-password")
    assert created["encrypted"] is True
    assert verified["valid"] is True


def test_real_stdio_mcp_process_lists_safe_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dagoberd.mcp_server"],
            env={
                **os.environ,
                "DAGOBERD_DB": str(tmp_path / "mcp-stdio.db"),
            },
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "system_status" in names
            assert "sync_comdirect" not in names
            assert {
                "transition_transaction_status",
                "suggest_transaction_relations",
                "review_transaction_relation",
                "record_balance_snapshot",
                "list_reconciliations",
                "create_manual_account",
                "search_transactions",
                "list_review_items",
                "preview_bulk_change",
                "run_bulk_change",
                "create_saved_analysis",
                "add_exchange_rate",
                "run_saved_analysis",
                "diagnose_comdirect",
                "export_portable_data",
                "create_encrypted_backup",
                "verify_portable_artifact",
                "check_data_integrity",
                "preview_data_deletion",
                "contract_versions",
                "preview_llm_classification",
                "execute_llm_classification_preview",
                "list_report_runs",
                "preview_report_delivery",
            }.issubset(names)
            status = await session.call_tool("system_status", {})
            assert status.isError is False

    asyncio.run(scenario())

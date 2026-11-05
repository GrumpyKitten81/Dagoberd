import json

import pytest

from dagoberd.cli import main
from dagoberd.config import ImportProfileConfig
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.mcp_server import MCPService
from dagoberd.services.import_service import ImportService


def test_first_and_follow_up_imports_are_scoped_and_repeatable(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    provider = repo.ensure_provider("file", "Synthetic bank")
    account, _ = repo.ensure_account(provider, "SYN-A", "Synthetic A", "EUR")
    other, _ = repo.ensure_account(provider, "SYN-B", "Synthetic B", "EUR")
    profile = ImportProfileConfig(source="tabular", delimiter=";")
    service = ImportService(repo, profiles={"synthetic": profile})
    path = tmp_path / "export.csv"
    path.write_text(
        "Datum;Betrag;Verwendungszweck;external_id\n2026-01-01;10;One;one\n", encoding="utf-8"
    )
    unscoped = service.preview("tabular", path, profile="synthetic")
    assert any(error["code"] == "account_required" for error in unscoped.errors)
    with pytest.raises(ValueError, match="validation errors"):
        service.execute(unscoped.preview_id)
    preview = service.preview("tabular", path, account, "synthetic")
    assert any("identity is missing" in warning for warning in preview.warnings)
    assert service.execute(preview.preview_id).imported == 1

    path.write_text(
        "Datum;Betrag;Verwendungszweck;external_id\n2026-01-01;10;One;one\n2026-01-02;20;Two;two\n",
        encoding="utf-8",
    )
    follow_up = service.preview("tabular", path, account, "synthetic")
    assert follow_up.duplicate_candidates == 1
    result = service.execute(follow_up.preview_id)
    assert (result.imported, result.skipped) == (1, 1)
    assert (
        service.execute(service.preview("tabular", path, account, "synthetic").preview_id).imported
        == 0
    )
    assert len(service.history(account)) == 3
    assert service.history(other) == []
    assert all("file_path" not in entry for entry in service.history(account))
    mcp = MCPService(db_path=str(tmp_path / "ledger.db"))
    assert len(mcp.execute("list_account_import_history", {"account_id": account})["imports"]) == 3
    with pytest.raises(ValueError, match="idempotency_key"):
        mcp.execute("preview_account_import", {"source": "tabular", "path": str(path)})
    arguments = {
        "source": "tabular",
        "path": str(path),
        "account_id": account,
        "idempotency_key": "synthetic-preview-1",
    }
    first_preview = mcp.execute("preview_account_import", arguments)
    assert (
        first_preview["preview_id"]
        == mcp.execute("preview_account_import", arguments)["preview_id"]
    )


def test_account_mismatch_and_changed_input_prevent_execution(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    provider = repo.ensure_provider("file", "Synthetic bank")
    account, _ = repo.ensure_account(provider, "SYN-A", "Synthetic A", "EUR")
    path = tmp_path / "export.csv"
    path.write_text(
        "Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;One;SYN-A\n2026-01-02;20;Two;SYN-B\n"
    )
    service = ImportService(repo)
    preview = service.preview("tabular", path, account)
    assert any(error["code"] == "account_mismatch" for error in preview.errors)
    with pytest.raises(ValueError, match="validation errors"):
        service.execute(preview.preview_id)
    path.write_text("Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;One;SYN-A\n")
    preview = service.preview("tabular", path, account)
    path.write_text(path.read_text() + "2026-01-02;20;Two;SYN-A\n")
    with pytest.raises(ValueError, match="changed after preview"):
        service.execute(preview.preview_id)
    assert repo.list_transactions() == []


def test_camt_mismatched_account_is_not_rerouted(tmp_path):
    from dagoberd.importers.camt053 import Camt053Importer

    path = tmp_path / "statement.xml"
    path.write_text(
        "<Document><Stmt><Acct><Id><IBAN>SYN-B</IBAN></Id></Acct>"
        "<Ntry><Amt Ccy='EUR'>1</Amt><CdtDbtInd>CRDT</CdtDbtInd>"
        "<BookgDt><Dt>2026-01-01</Dt></BookgDt></Ntry></Stmt></Document>"
    )
    batch = Camt053Importer().inspect_transactions(path, "SYN-A")
    assert any(issue.code == "account_mismatch" for issue in batch.issues)


def test_multi_account_history_and_repeated_identities(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    path = tmp_path / "multi.csv"
    path.write_text(
        "Datum;Betrag;Verwendungszweck;Konto;external_id\n"
        "2026-01-01;10;One;SYN-A;one\n"
        "2026-01-02;20;Two;SYN-B;two\n"
    )
    service = ImportService(repo)
    preview = service.preview("tabular", path)
    service.execute(preview.preview_id)
    accounts = repo.list_accounts()
    assert len(accounts) == 2
    for account in accounts:
        history = service.history(account["id"])
        assert len(history) == 1
        assert history[0]["counts_scope"] == "entire_import"
        assert history[0]["imported"] == 2
    with path.open("a") as handle:
        handle.write("2026-01-02;20;Two;SYN-B;two\n")
    assert any(
        "repeated source identities" in warning
        for warning in service.preview("tabular", path).warnings
    )
    separate = LedgerRepository(LedgerDatabase(tmp_path / "other.db"))
    provider = separate.ensure_provider("file", "Dateiimport")
    other_account, _ = separate.ensure_account(provider, "SYN-A", "Other workspace", "EUR")
    assert ImportService(separate).history(other_account) == []


def test_synthetic_cli_journey_and_changed_profile(tmp_path, capsys):
    registry = tmp_path / "registry.toml"
    workspace = tmp_path / "workspace"
    prefix = ["--workspace-registry", str(registry)]

    def run(*args):
        assert main([*prefix, *args]) == 0
        return json.loads(capsys.readouterr().out)

    run("workspaces", "create", "demo", str(workspace))
    scoped = ["--workspace", "demo"]
    run(*scoped, "accounts", "register", "file", "SyntheticBank", "SYN-A", "Synthetic")
    account = run(*scoped, "accounts", "list")[0]["id"]
    config = workspace / "account-eval.toml"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            '\n[imports.profiles.synthetic_account]\nsource = "tabular"\n'
            'delimiter = ";"\n[imports.profiles.synthetic_account.mapping]\n'
            'external_id = ["ID"]\nbooking_date = ["Date"]\namount = ["Amount"]\n'
            'purpose = ["Purpose"]\naccount = ["Account"]\n'
        )
    path = workspace / "export.csv"
    path.write_text("ID;Date;Amount;Purpose;Account\none;2026-01-01;10;Income;SYN-A\n")
    args = [
        *scoped,
        "import",
        "preview",
        "--source",
        "tabular",
        "--account",
        account,
        "--profile",
        "synthetic_account",
        str(path),
    ]
    first = run(*args)
    assert first["error_count"] == 0
    assert run(*scoped, "import", "run", first["preview_id"])["imported"] == 1
    with path.open("a") as handle:
        handle.write("two;2026-01-02;-2;Purchase;SYN-A\n")
    follow_up = run(*args)
    assert follow_up["duplicate_candidates"] == 1
    assert run(*scoped, "import", "run", follow_up["preview_id"])["imported"] == 1
    assert len(run(*scoped, "import", "history", "--account", account)) == 2

    stale = run(*args)
    config.write_text(
        config.read_text(encoding="utf-8").replace('amount = ["Amount"]', 'amount = ["Changed"]'),
        encoding="utf-8",
    )
    assert main([*prefix, *scoped, "import", "run", stale["preview_id"]]) != 0
    capsys.readouterr()
    assert run(*args)["error_count"] > 0
    unsupported = workspace / "export.pdf"
    unsupported.write_text("synthetic")
    assert (
        run(
            *scoped,
            "import",
            "preview",
            "--source",
            "tabular",
            "--account",
            account,
            str(unsupported),
        )["errors"][0]["code"]
        == "unsupported_format"
    )

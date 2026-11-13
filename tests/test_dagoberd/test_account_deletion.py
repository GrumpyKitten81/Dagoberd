from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dagoberd.cli import main
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.services.account_deletion import (
    DELETABLE_TABLES,
    PROTECTED_TABLES,
    AccountDeletionService,
)
from dagoberd.services.import_service import ImportService


@pytest.fixture
def setup(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    org = OrganizationService(repo)
    account = org.create_manual_account(
        "Synthetic", account_kind="checking", idempotency_key="synthetic-account"
    )
    service = AccountDeletionService(repo, tmp_path / ".dagoberd")
    return repo, org, account["id"], service


def manual_history(setup, status="reversed", resolve=True):
    repo, org, account, _ = setup
    transaction = org.create_manual_transaction(
        account,
        booking_date=date(2026, 1, 1),
        amount=Decimal("10"),
        purpose="Synthetic",
        idempotency_key="synthetic-entry",
        booking_status=status,
    )
    if resolve:
        for item in org.list_review_items():
            org.review_item(item["id"], "resolve", expected_version=item["version"])
    repo.set_account_enabled(account, False)
    return transaction


def execute(service, preview):
    return service.execute(preview["preview_id"], preview["fingerprint"])


def test_manual_deletion_is_durable_and_idempotent(setup):
    repo, _, account, service = setup
    transaction = manual_history(setup)
    preview = service.preview(account)
    assert preview["action"] == "delete", preview
    assert preview["transactions"]["count"] == 1
    assert "state" not in preview
    assert "manual-source:" not in json.dumps(preview)
    result = execute(service, preview)
    assert result["action"] == "delete"
    assert result["changed"]
    receipt = Path(result["receipt"])
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert execute(service, preview) == result
    with pytest.raises(KeyError):
        repo.get_account(account)
    with repo.database.connect() as connection:
        assert not connection.execute("SELECT * FROM ledger_transactions").fetchall()
        assert not connection.execute("SELECT * FROM ledger_source_transactions").fetchall()
        assert connection.execute(
            "SELECT 1 FROM ledger_audit_events WHERE entity_id=?", (transaction["id"],)
        ).fetchone()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("provider", ["file", "comdirect", "manual"])
def test_empty_disabled_accounts_can_be_deleted(setup, provider):
    repo, _, _, service = setup
    provider_id = repo.ensure_provider(provider, "Synthetic bank")
    account, _ = repo.ensure_account(provider_id, "SYN-A", "Empty", "EUR")
    repo.set_account_enabled(account, False)
    preview = service.preview(account)
    assert preview["action"] == "delete"
    execute(service, preview)


@pytest.mark.parametrize("status", ["booked", "pending", "corrected"])
def test_non_reversed_history_is_blocked_but_fallback_preserves_it(setup, status):
    repo, _, account, service = setup
    manual_history(setup, status)
    assert "non_reversed_transactions" in service.preview(account)["blockers"]
    preview = service.preview(account, cleanup_references=True, fallback="disable")
    result = execute(service, preview)
    assert result["action"] == "disable"
    assert not result["changed"]
    with repo.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0] == 1


def test_active_account_fallback_and_stale_plan(setup):
    repo, _, account, service = setup
    preview = service.preview(account, fallback="disable")
    assert preview["action"] == "disable"
    repo.update_account_alias(account, "Changed")
    with pytest.raises(ValueError, match="changed"):
        execute(service, preview)
    assert repo.get_account(account)["enabled"]
    current = service.preview(account, fallback="disable")
    assert execute(service, current)["changed"]
    assert not repo.get_account(account)["enabled"]
    assert account not in {row["id"] for row in repo.list_accounts(enabled_only=True)}
    assert account in {row["id"] for row in repo.list_accounts()}


def test_disabled_file_import_and_old_preview_cannot_book(setup, tmp_path):
    repo, _, _, _ = setup
    provider = repo.ensure_provider("file", "Dateiimport")
    account, _ = repo.ensure_account(provider, "SYN-A", "File", "EUR")
    path = tmp_path / "export.csv"
    path.write_text("Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;Synthetic;SYN-A\n")
    imports = ImportService(repo)
    preview = imports.preview("tabular", path, account)
    repo.set_account_enabled(account, False)
    with pytest.raises(ValueError, match="disabled"):
        imports.execute(preview.preview_id)
    # Automatic source-account discovery must not bypass the target guard.
    automatic = imports.preview("tabular", path)
    assert any(issue["code"] == "account_disabled" for issue in automatic.errors)
    with pytest.raises(ValueError, match="validation errors"):
        imports.execute(automatic.preview_id)
    assert repo.get_account(account)["enabled"] == 0
    with repo.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0] == 0


def test_manual_account_with_imported_history_is_protected(setup, tmp_path):
    repo, _, account, service = setup
    external = repo.get_account(account)["external_id"]
    path = tmp_path / "export.csv"
    path.write_text(f"Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;Synthetic;{external}\n")
    imports = ImportService(repo)
    imports.execute(imports.preview("tabular", path, account).preview_id)
    with repo.database.connect() as connection:
        connection.execute("UPDATE ledger_transactions SET booking_status='reversed'")
        connection.execute("UPDATE ledger_review_items SET status='resolved'")
    repo.set_account_enabled(account, False)
    preview = service.preview(account)
    assert preview["action"] == "blocked"
    assert "unproven_manual_origin" in preview["blockers"]
    with pytest.raises(ValueError, match="blocked"):
        execute(service, preview)


def test_open_reviews_and_new_dependents_invalidate_deletion(setup):
    repo, org, account, service = setup
    manual_history(setup, resolve=False)
    assert "open_review_items" in service.preview(account)["blockers"]
    for item in org.list_review_items():
        org.review_item(item["id"], "resolve", expected_version=item["version"])
    preview = service.preview(account)
    with repo.database.connect() as connection:
        connection.execute(
            "INSERT INTO ledger_account_stages(account_id, stage, enabled) VALUES (?, 'rule', 0)",
            (account,),
        )
    with pytest.raises(ValueError, match="changed"):
        execute(service, preview)


def test_cleanup_keeps_document_files_and_updates_analysis_selection(setup, tmp_path):
    repo, org, account, service = setup
    transaction = manual_history(setup)
    other = org.create_manual_account(
        "Other", account_kind="checking", idempotency_key="synthetic-other"
    )["id"]
    document = tmp_path / "receipt.txt"
    document.write_text("Synthetic")
    with repo.database.connect() as connection:
        connection.execute(
            "INSERT INTO ledger_documents(id, filename, storage_path) VALUES ('doc', 'receipt.txt', ?)",
            (str(document),),
        )
        connection.execute(
            """INSERT INTO ledger_document_matches(id, document_id, transaction_id, score,
               reasons_json, status) VALUES ('match', 'doc', ?, 1, '[]', 'matched')""",
            (transaction["id"],),
        )
        connection.execute(
            """INSERT INTO ledger_saved_analyses(id, code, name, definition_json)
               VALUES ('analysis', 'synthetic', 'Synthetic', ?)""",
            (json.dumps({"account_scope": {"mode": "selected", "account_ids": [account, other]}}),),
        )
    assert service.preview(account)["action"] == "blocked"
    preview = service.preview(account, cleanup_references=True)
    assert preview["action"] == "delete", preview
    execute(service, preview)
    with repo.database.connect() as connection:
        analysis = connection.execute("SELECT * FROM ledger_saved_analyses").fetchone()
        assert analysis["version"] == 2
        assert json.loads(analysis["definition_json"])["account_scope"]["account_ids"] == [other]
        assert connection.execute("SELECT COUNT(*) FROM ledger_documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger_document_matches").fetchone()[0] == 0
    assert document.read_text() == "Synthetic"
    assert repo.get_account(other)["enabled"]


def test_last_analysis_account_blocks_even_with_cleanup(setup):
    repo, _, account, service = setup
    repo.set_account_enabled(account, False)
    with repo.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_saved_analyses(id, code, name, definition_json)
               VALUES ('analysis', 'synthetic', 'Synthetic', ?)""",
            (json.dumps({"account_scope": {"mode": "selected", "account_ids": [account]}}),),
        )
    preview = service.preview(account, cleanup_references=True)
    assert "empty_or_unsupported_analysis_scope" in preview["blockers"]


def test_rollback_and_receipt_publication_recovery(setup, monkeypatch, tmp_path):
    repo, _, account, service = setup
    manual_history(setup)
    preview = service.preview(account)
    original_apply = service._apply

    def fail_after_apply(connection, plan):
        original_apply(connection, plan)
        raise RuntimeError("Synthetic interruption")

    monkeypatch.setattr(service, "_apply", fail_after_apply)
    with pytest.raises(RuntimeError):
        execute(service, preview)
    assert repo.get_account(account)
    monkeypatch.setattr(service, "_apply", original_apply)
    service.metadata_dir = tmp_path / "blocked"
    service.metadata_dir.write_text("Synthetic file prevents directory creation")
    result = execute(service, preview)
    assert result["receipt"] is None
    with pytest.raises(KeyError):
        repo.get_account(account)
    service.metadata_dir.unlink()
    recovered = execute(service, preview)
    assert Path(recovered["receipt"]).is_file()
    assert recovered["executed_at"] == result["executed_at"]


def test_disabled_balances_manual_writes_and_routed_imports(setup, tmp_path):
    repo, org, account, service = setup
    execute(service, service.preview(account, fallback="disable"))
    with pytest.raises(ValueError, match="disabled"):
        repo.record_balance_snapshot(account, 100)
    with pytest.raises(ValueError, match="disabled"):
        org.create_manual_transaction(
            account,
            booking_date=date(2026, 1, 1),
            amount=Decimal("1"),
            purpose="Synthetic",
            idempotency_key="disabled",
        )
    provider = repo.ensure_provider("file", "Dateiimport")
    source, _ = repo.ensure_account(provider, "SOURCE", "Source", "EUR")
    target, _ = repo.ensure_account(provider, "TARGET", "Target", "EUR")
    repo.set_account_enabled(source, False)
    with repo.database.connect() as connection:
        connection.execute(
            "INSERT INTO ledger_account_source_routes VALUES (?, ?, CURRENT_TIMESTAMP)",
            (source, target),
        )
    path = tmp_path / "source.csv"
    path.write_text("Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;Synthetic;SOURCE\n")
    imports = ImportService(repo)
    assert imports.execute(imports.preview("tabular", path).preview_id).imported == 1
    assert "protected_history:ledger_account_source_routes" in service.preview(source)["blockers"]
    repo.set_account_enabled(target, False)
    with pytest.raises(ValueError, match="validation errors"):
        imports.execute(imports.preview("tabular", path).preview_id)


def test_schema_reference_inventory_is_complete(setup):
    repo, _, _, _ = setup
    covered = DELETABLE_TABLES | PROTECTED_TABLES
    with repo.database.connect() as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for key in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                if key[2] in covered:
                    assert table in covered, (table, key[2])


def test_unknown_dependency_fails_closed(setup):
    repo, _, account, service = setup
    repo.set_account_enabled(account, False)
    with repo.database.connect() as connection:
        connection.execute(
            "CREATE TABLE ledger_future(id TEXT PRIMARY KEY, account_id TEXT REFERENCES ledger_accounts(id))"
        )
        connection.execute("INSERT INTO ledger_future VALUES ('future', ?)", (account,))
    assert "unsupported_dependency:ledger_future" in service.preview(account)["blockers"]


def test_cli_confirmation_and_restart_receipt(tmp_path, capsys):
    registry = tmp_path / "registry.toml"
    prefix = ["--workspace-registry", str(registry)]

    def run(*args):
        assert main([*prefix, *args]) == 0
        return json.loads(capsys.readouterr().out)

    run("workspaces", "create", "demo", str(tmp_path / "workspace"))
    prefix += ["--workspace", "demo"]
    account = run(
        "accounts",
        "create-manual",
        "Synthetic",
        "--type",
        "checking",
        "--idempotency-key",
        "synthetic-demo",
    )["id"]
    preview = run("accounts", "delete-preview", account, "--fallback", "disable")
    args = [
        "accounts",
        "delete-run",
        preview["preview_id"],
        "--fingerprint",
        preview["fingerprint"],
    ]
    assert main([*prefix, *args]) != 0
    capsys.readouterr()
    disabled = run(*args, "--yes")
    assert disabled["action"] == "disable"
    preview = run("accounts", "delete-preview", account)
    args = [
        "accounts",
        "delete-run",
        preview["preview_id"],
        "--fingerprint",
        preview["fingerprint"],
        "--yes",
    ]
    receipt = run(*args)
    assert receipt["action"] == "delete"
    assert run(*args) == receipt
    assert run("accounts", "list") == []


def test_void_history_is_allowed_and_financial_payload_is_not_retained(setup):
    repo, _, account, service = setup
    transaction = manual_history(setup, "booked")
    with repo.database.connect() as connection:
        connection.execute(
            "UPDATE ledger_transactions SET state='void' WHERE id=?", (transaction["id"],)
        )
    preview = service.preview(account)
    assert preview["action"] == "delete"
    execute(service, preview)
    with repo.database.connect() as connection:
        stored = connection.execute("SELECT plan_json FROM ledger_account_deletions").fetchone()[0]
        assert "raw_payload_json" not in stored
        assert "manual-source:" not in stored


@pytest.mark.parametrize("relation_status,blocked", [("confirmed", True), ("suggested", False)])
def test_cross_account_relationship_policy(setup, relation_status, blocked):
    repo, org, account, service = setup
    transaction = manual_history(setup)
    other = org.create_manual_account(
        "Other", account_kind="checking", idempotency_key="other-account"
    )["id"]
    other_transaction = org.create_manual_transaction(
        other,
        booking_date=date(2026, 1, 1),
        amount=Decimal("-10"),
        purpose="Other",
        idempotency_key="other-transaction",
    )
    with repo.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_transaction_relations(id, relation_type,
               source_transaction_id, target_transaction_id, status, source_version, target_version)
               VALUES ('relation', 'reversal', ?, ?, ?, 1, 1)""",
            (transaction["id"], other_transaction["id"], relation_status),
        )
    preview = service.preview(account, cleanup_references=True)
    assert ("confirmed_cross_account_relation" in preview["blockers"]) == blocked
    if not blocked:
        execute(service, preview)
    with repo.database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM ledger_transactions WHERE id=?", (other_transaction["id"],)
        ).fetchone()


def test_active_bulk_preview_blocks_deletion(setup):
    repo, _, account, service = setup
    transaction = manual_history(setup)
    with repo.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_bulk_previews(id, action, payload_json, transaction_count,
               version_fingerprint) VALUES ('bulk', 'tag_add', '{}', 1, 'synthetic')"""
        )
        connection.execute(
            "INSERT INTO ledger_bulk_preview_items VALUES ('bulk', ?, 1)", (transaction["id"],)
        )
    assert "operational_reference:ledger_bulk_previews" in service.preview(account)["blockers"]
    with repo.database.connect() as connection:
        connection.execute("UPDATE ledger_bulk_previews SET status='executed'")
    execute(service, service.preview(account))
    with repo.database.connect() as connection:
        assert connection.execute("SELECT 1 FROM ledger_bulk_previews WHERE id='bulk'").fetchone()
        assert not connection.execute("SELECT 1 FROM ledger_bulk_preview_items").fetchone()


def test_wrong_fingerprint_foreign_ledger_and_writer_conflict(setup, tmp_path, monkeypatch):
    repo, _, account, service = setup
    repo.set_account_enabled(account, False)
    preview = service.preview(account)
    with pytest.raises(ValueError, match="fingerprint"):
        service.execute(preview["preview_id"], "wrong")
    other = LedgerRepository(LedgerDatabase(tmp_path / "other.db"))
    with repo.database.connect() as connection, other.database.connect() as destination:
        stored = connection.execute("SELECT * FROM ledger_account_deletions").fetchone()
        destination.execute(
            "INSERT INTO ledger_account_deletions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(stored)
        )
    with pytest.raises(ValueError, match="another workspace or ledger"):
        execute(AccountDeletionService(other, tmp_path / "other-metadata"), preview)
    original = repo.database.connect

    def immediate_connection():
        connection = original()
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    monkeypatch.setattr(repo.database, "connect", immediate_connection)
    with repo.database.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            execute(service, preview)
    assert repo.get_account(account)
    execute(service, preview)


def test_additional_foreign_source_is_protected(setup):
    repo, org, account, service = setup
    transaction = manual_history(setup)
    other = org.create_manual_account(
        "Other", account_kind="checking", idempotency_key="other-account"
    )["id"]
    other_transaction = org.create_manual_transaction(
        other,
        booking_date=date(2026, 1, 1),
        amount=Decimal("10"),
        purpose="Other",
        idempotency_key="other-transaction",
        booking_status="reversed",
    )
    with repo.database.connect() as connection:
        connection.execute(
            "INSERT INTO ledger_transaction_sources(transaction_id, source_transaction_id) VALUES (?, ?)",
            (transaction["id"], other_transaction["source_transaction_id"]),
        )
    preview = service.preview(account)
    assert "unproven_manual_origin" in preview["blockers"]
    assert "shared_source_history" in preview["blockers"]


def test_schema_v20_upgrade_keeps_existing_accounts(setup):
    repo, _, account, _ = setup
    with repo.database.connect() as connection:
        connection.execute("DROP TABLE ledger_account_deletions")
        connection.execute("DELETE FROM schema_migrations WHERE version=21")
    repo.database.initialize()
    assert repo.get_account(account)["enabled"]
    with repo.database.connect() as connection:
        assert connection.execute("SELECT * FROM ledger_account_deletions").fetchall() == []


def test_split_manual_history_and_opening_balance_are_deleted_together(setup):
    repo, _, account, service = setup
    transaction = manual_history(setup, "booked")
    repo.set_account_enabled(account, True)
    repo.record_balance_snapshot(account, 100, balance_date="2025-12-31", source="manual-opening")
    repo.split_transaction(
        transaction["id"],
        "synthetic",
        [(Decimal("4"), None, None), (Decimal("6"), None, None)],
        transaction["version"],
    )
    with repo.database.connect() as connection:
        connection.execute(
            "UPDATE ledger_transactions SET booking_status='reversed' WHERE id=?",
            (transaction["id"],),
        )
    repo.set_account_enabled(account, False)
    preview = service.preview(account)
    assert preview["action"] == "delete", preview
    assert preview["changes"]["delete_rows"]["ledger_splits"] == 2
    assert preview["changes"]["delete_rows"]["ledger_balance_snapshots"] == 1
    execute(service, preview)
    with repo.database.connect() as connection:
        assert not connection.execute("SELECT 1 FROM ledger_splits").fetchone()
        assert not connection.execute("SELECT 1 FROM ledger_balance_snapshots").fetchone()


def test_automatic_import_does_not_replace_disabled_identity_in_other_provider(setup, tmp_path):
    repo, _, _, _ = setup
    provider = repo.ensure_provider("file", "Registered synthetic bank")
    account, _ = repo.ensure_account(provider, "SYN-A", "Retired", "EUR")
    repo.set_account_enabled(account, False)
    path = tmp_path / "export.csv"
    path.write_text("Datum;Betrag;Verwendungszweck;Konto\n2026-01-01;10;Synthetic;SYN-A\n")
    imports = ImportService(repo)
    preview = imports.preview("tabular", path)
    assert any(issue["code"] == "account_disabled" for issue in preview.errors)
    with pytest.raises(ValueError, match="validation errors"):
        imports.execute(preview.preview_id)
    with repo.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ledger_accounts WHERE external_id='SYN-A'"
            ).fetchone()[0]
            == 1
        )

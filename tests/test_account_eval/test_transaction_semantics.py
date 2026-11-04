from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from account_eval.accounts import AccountScope
from account_eval.cli import main
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction, TransactionStatus
from account_eval.ledger.repository import LedgerRepository, VersionConflictError
from account_eval.ledger.semantics import TransactionSemanticsService
from account_eval.mcp_server import TOOL_DEFINITIONS
from account_eval.reporting.ledger_reports import LedgerReportGenerator


@pytest.fixture
def repository(tmp_path) -> LedgerRepository:
    return LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))


def booking(
    external_id: str,
    account: str,
    amount: str,
    *,
    booking_date: date = date(2026, 7, 1),
    purpose: str = "Eigener Transfer",
    status: TransactionStatus = TransactionStatus.BOOKED,
) -> NormalizedTransaction:
    return NormalizedTransaction(
        external_id=external_id,
        account_external_id=account,
        booking_date=booking_date,
        amount=Decimal(amount),
        currency="EUR",
        purpose=purpose,
        reference=external_id,
        raw={"id": external_id, "status": status.value},
        booking_status=status,
    )


def transaction_by_reference(repository: LedgerRepository, reference: str) -> dict:
    return next(
        item
        for item in repository.list_transactions(
            limit=None,
            booking_statuses=None,
        )
        if item["reference"] == reference
    )


def test_provider_revision_advances_pending_to_booked_with_history(repository):
    first = repository.import_transactions(
        "file",
        "test",
        [booking("one", "account", "10.00", status=TransactionStatus.PENDING)],
    )
    second = repository.import_transactions(
        "file",
        "test",
        [
            booking(
                "one",
                "account",
                "10.00",
                booking_date=date(2026, 7, 2),
                purpose="Final gebucht",
                status=TransactionStatus.BOOKED,
            )
        ],
    )

    assert (first.imported, first.updated) == (1, 0)
    assert (second.imported, second.skipped, second.updated) == (0, 0, 1)
    row = transaction_by_reference(repository, "one")
    assert row["booking_status"] == "booked"
    assert row["booking_date"] == "2026-07-02"
    assert row["purpose"] == "Final gebucht"
    history = repository.list_transaction_status_history(row["id"])
    assert [(item["from_status"], item["to_status"]) for item in history] == [
        (None, "pending"),
        ("pending", "booked"),
    ]
    with repository.database.connect() as connection:
        revisions = connection.execute(
            """SELECT normalized_payload_json
               FROM ledger_source_transaction_revisions
               ORDER BY imported_at, id"""
        ).fetchall()
    assert len(revisions) == 2
    revision_dates = {
        json.loads(item["normalized_payload_json"])["booking_date"] for item in revisions
    }
    assert revision_dates == {"2026-07-01", "2026-07-02"}


def test_status_transitions_are_versioned_terminal_and_split_safe(repository):
    repository.import_transactions("file", "test", [booking("one", "account", "100.00")])
    row = transaction_by_reference(repository, "one")
    next_version = repository.transition_transaction_status(
        row["id"],
        "corrected",
        row["version"],
        reason="Durch Ersatzbuchung korrigiert",
    )
    assert next_version == row["version"] + 1
    with pytest.raises(ValueError, match="Invalid transaction status transition"):
        repository.transition_transaction_status(
            row["id"],
            "booked",
            next_version,
            reason="Unzulässiger Rücksprung",
        )
    with pytest.raises(VersionConflictError):
        repository.transition_transaction_status(
            row["id"],
            "reversed",
            row["version"],
            reason="Veraltete Version",
        )

    repository.import_transactions("file", "test", [booking("split", "account", "100.00")])
    split = transaction_by_reference(repository, "split")
    repository.split_transaction(
        split["id"],
        "rental",
        [(Decimal("60"), None, None), (Decimal("40"), None, None)],
        split["version"],
    )
    current = repository.get_transaction(split["id"])
    with pytest.raises(ValueError, match="Revert the split"):
        repository.transition_transaction_status(
            split["id"],
            "reversed",
            current["version"],
            reason="Nicht vor Split-Rücknahme",
        )


def test_transfer_candidate_fee_confirmation_and_resolution_affect_cashflow(
    repository,
    tmp_path,
):
    repository.import_transactions(
        "file",
        "test",
        [
            booking("out", "checking", "-100.00"),
            booking("in", "savings", "99.00"),
        ],
    )
    relations = TransactionSemanticsService(repository).suggest("transfer")

    assert len(relations) == 1
    relation = relations[0]
    assert relation["fee_amount_minor"] == 100
    confirmed = repository.review_transaction_relation(
        relation["id"], "confirm", relation["version"]
    )
    rows = repository.list_transactions(limit=None)
    assert sum(item["cashflow_amount_minor"] for item in rows) == -100
    assert all(item["is_internal_transfer"] for item in rows)

    reports = LedgerReportGenerator(repository, tmp_path / "reports").generate(
        formats=("json", "xlsx", "html"),
    )
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["months"][0]["Saldo"] == "-1"
    assert payload["metadata"]["cashflow_excluded_count"] == 2
    months = pd.read_excel(reports[1], sheet_name="Months")
    assert months.loc[0, "Balance"] == -1
    html = reports[2].read_text(encoding="utf-8")
    assert "Cashflow" in html
    assert "-1" in html

    resolved = repository.review_transaction_relation(
        relation["id"], "resolve", confirmed["version"]
    )
    assert resolved["status"] == "resolved"
    assert (
        sum(item["cashflow_amount_minor"] for item in repository.list_transactions(limit=None))
        == -100
    )


def test_refund_reversal_and_correction_have_distinct_report_semantics(repository):
    repository.import_transactions(
        "file",
        "test",
        [
            booking("expense", "checking", "-50.00", purpose="Shop A"),
            booking(
                "refund",
                "checking",
                "50.00",
                booking_date=date(2026, 7, 2),
                purpose="Shop A Erstattung",
            ),
            booking("original", "card", "20.00", purpose="Fehlbuchung"),
            booking(
                "reversal",
                "card",
                "-20.00",
                booking_date=date(2026, 7, 2),
                purpose="Fehlbuchung Storno",
            ),
            booking("old", "loan", "-30.00", purpose="Korrektur"),
            booking(
                "replacement",
                "loan",
                "-33.00",
                booking_date=date(2026, 7, 2),
                purpose="Korrektur ersetzt",
            ),
        ],
    )
    service = TransactionSemanticsService(repository)
    refund = service.suggest(
        "refund", transaction_id=transaction_by_reference(repository, "expense")["id"]
    )[0]
    reversal = service.suggest(
        "reversal",
        transaction_id=transaction_by_reference(repository, "original")["id"],
    )[0]
    correction = service.suggest(
        "correction",
        transaction_id=transaction_by_reference(repository, "old")["id"],
    )[0]

    repository.review_transaction_relation(refund["id"], "confirm", refund["version"])
    repository.review_transaction_relation(reversal["id"], "confirm", reversal["version"])
    repository.review_transaction_relation(correction["id"], "confirm", correction["version"])

    assert transaction_by_reference(repository, "original")["booking_status"] == "reversed"
    assert transaction_by_reference(repository, "old")["booking_status"] == "corrected"
    visible = repository.list_transactions(limit=None)
    amounts = {item["reference"]: item["cashflow_amount_minor"] for item in visible}
    assert amounts["expense"] == -5000
    assert amounts["refund"] == 5000
    assert amounts["reversal"] == 0
    assert amounts["replacement"] == -3300
    assert repository.transaction_status_counts() == {
        "booked": 4,
        "corrected": 1,
        "reversed": 1,
    }


def test_confirmed_partial_refunds_cannot_exceed_original_expense(repository):
    repository.import_transactions(
        "file",
        "test",
        [
            booking("expense", "checking", "-100.00", purpose="Shop A"),
            booking(
                "refund-one",
                "checking",
                "60.00",
                booking_date=date(2026, 7, 2),
                purpose="Shop A Erstattung",
            ),
            booking(
                "refund-two",
                "checking",
                "60.00",
                booking_date=date(2026, 7, 3),
                purpose="Shop A Erstattung",
            ),
        ],
    )
    service = TransactionSemanticsService(repository)
    first = service.suggest(
        "refund",
        transaction_id=transaction_by_reference(repository, "refund-one")["id"],
    )[0]
    repository.review_transaction_relation(first["id"], "confirm", first["version"])
    second = service.suggest(
        "refund",
        transaction_id=transaction_by_reference(repository, "refund-two")["id"],
    )[0]

    with pytest.raises(ValueError, match="must not exceed"):
        repository.review_transaction_relation(second["id"], "confirm", second["version"])


def test_balance_snapshots_reconcile_against_booked_account_movements(repository):
    repository.import_transactions(
        "file",
        "test",
        [
            booking(
                "income",
                "checking",
                "100.00",
                booking_date=date(2026, 7, 2),
            )
        ],
    )
    account = repository.list_accounts()[0]
    baseline = repository.record_balance_snapshot(
        account["id"],
        100000,
        balance_date="2026-07-01",
        expected_account_version=account["version"],
    )
    assert baseline["reconciliation"]["status"] == "baseline"
    current = repository.get_account(account["id"])
    balanced = repository.record_balance_snapshot(
        account["id"],
        110000,
        balance_date="2026-07-02",
        expected_account_version=current["version"],
    )
    assert balanced["reconciliation"]["status"] == "balanced"
    assert balanced["reconciliation"]["difference_minor"] == 0
    assert balanced["reconciliation"]["transaction_count"] == 1

    current = repository.get_account(account["id"])
    difference = repository.record_balance_snapshot(
        account["id"],
        111000,
        balance_date="2026-07-03",
        expected_account_version=current["version"],
    )
    assert difference["reconciliation"]["status"] == "difference"
    assert difference["reconciliation"]["difference_minor"] == 1000

    repository.import_transactions(
        "file",
        "test",
        [
            booking(
                "late-income",
                "checking",
                "10.00",
                booking_date=date(2026, 7, 3),
            )
        ],
    )
    refreshed = repository.reconcile_balance_snapshot(
        difference["id"],
        refresh=True,
    )
    assert refreshed["status"] == "balanced"
    assert refreshed["difference_minor"] == 0
    assert refreshed["version"] == 2


def test_reconciliation_counts_both_sides_of_confirmed_reversal(repository):
    repository.import_transactions(
        "file",
        "test",
        [
            booking(
                "original",
                "checking",
                "20.00",
                booking_date=date(2026, 7, 2),
                purpose="Kartenreservierung",
            ),
            booking(
                "reversal",
                "checking",
                "-20.00",
                booking_date=date(2026, 7, 3),
                purpose="Kartenreservierung Storno",
            ),
        ],
    )
    account = repository.list_accounts()[0]
    repository.record_balance_snapshot(
        account["id"],
        100000,
        balance_date="2026-07-01",
        expected_account_version=account["version"],
    )
    relation = TransactionSemanticsService(repository).suggest("reversal")[0]
    repository.review_transaction_relation(relation["id"], "confirm", relation["version"])
    current = repository.get_account(account["id"])

    snapshot = repository.record_balance_snapshot(
        account["id"],
        100000,
        balance_date="2026-07-04",
        expected_account_version=current["version"],
    )

    assert snapshot["reconciliation"]["status"] == "balanced"
    assert snapshot["reconciliation"]["transaction_count"] == 2


def test_cli_and_mcp_expose_phase_one_semantics(repository, capsys):
    repository.import_transactions(
        "file",
        "test",
        [booking("pending", "checking", "10.00", status=TransactionStatus.PENDING)],
    )
    row = transaction_by_reference(repository, "pending")
    assert (
        main(
            [
                "--db",
                str(repository.database.path),
                "transaction-status",
                row["id"],
                "booked",
                "--version",
                str(row["version"]),
                "--reason",
                "Bank hat final gebucht",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["booking_status"] == "booked"

    definitions = {name: schema for name, _description, schema in TOOL_DEFINITIONS}
    assert {
        "transition_transaction_status",
        "suggest_transaction_relations",
        "review_transaction_relation",
        "record_balance_snapshot",
        "list_reconciliations",
    }.issubset(definitions)


def test_account_scope_limits_relation_candidates(repository):
    repository.import_transactions(
        "file",
        "test",
        [
            booking("out", "one", "-10.00"),
            booking("in", "two", "10.00"),
            booking("other", "three", "10.00"),
        ],
    )
    accounts = {item["external_id"]: item["id"] for item in repository.list_accounts()}
    scope = AccountScope.selected([accounts["one"], accounts["two"]])
    relations = TransactionSemanticsService(repository).suggest("transfer", account_scope=scope)
    assert len(relations) == 1
    assert {
        relations[0]["source_transaction_id"],
        relations[0]["target_transaction_id"],
    } == {
        transaction_by_reference(repository, "out")["id"],
        transaction_by_reference(repository, "in")["id"],
    }

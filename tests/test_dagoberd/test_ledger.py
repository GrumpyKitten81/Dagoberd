import hashlib
from datetime import date
from decimal import Decimal

import pytest

from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction, from_minor_units, to_minor_units
from dagoberd.ledger.repository import (
    LedgerRepository,
    SourceTransactionConflictError,
    VersionConflictError,
)
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.ledger_reports import LedgerReportGenerator


@pytest.fixture
def repository(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repo)
    return repo


def transaction(reference="one", purpose="Miete Juli", amount=Decimal("266.00")):
    return NormalizedTransaction(
        reference, date(2026, 7, 1), amount, "EUR", purpose, "account-1", reference=reference
    )


def test_decimal_minor_units_do_not_use_float():
    assert to_minor_units(Decimal("0.29")) == 29
    assert from_minor_units(29) == Decimal("0.29")


def test_current_schema_and_builtin_profiles_do_not_rewrite_database(tmp_path):
    database_path = tmp_path / "stable-ledger.db"
    repository = LedgerRepository(LedgerDatabase(database_path))
    install_builtin_profiles(repository)
    before = hashlib.sha256(database_path.read_bytes()).digest()

    second_repository = LedgerRepository(LedgerDatabase(database_path))
    install_builtin_profiles(second_repository)

    assert hashlib.sha256(database_path.read_bytes()).digest() == before


def test_import_is_idempotent(repository):
    first = repository.import_transactions("file", "test", [transaction()])
    second = repository.import_transactions("file", "test", [transaction()])
    assert (first.imported, first.skipped, first.accounts) == (1, 0, 1)
    assert (second.imported, second.skipped) == (0, 1)


def test_import_rejects_changed_source_data_for_the_same_identity(repository):
    repository.import_transactions("file", "test", [transaction()])

    with pytest.raises(SourceTransactionConflictError, match="Source transaction changed"):
        repository.import_transactions(
            "file",
            "test",
            [transaction(amount=Decimal("267.00"))],
        )

    assert len(repository.list_transactions()) == 1


def test_conflicting_import_rolls_back_the_entire_batch(repository):
    repository.import_transactions("file", "test", [transaction()])

    with pytest.raises(SourceTransactionConflictError):
        repository.import_transactions(
            "file",
            "test",
            [
                transaction("new", amount=Decimal("10.00")),
                transaction(amount=Decimal("999.00")),
            ],
        )

    assert {row["reference"] for row in repository.list_transactions()} == {"one"}


def test_import_audits_created_ledger_entities(repository):
    repository.import_transactions("file", "test", [transaction()])

    with repository.database.connect() as connection:
        entity_types = {
            row["entity_type"]
            for row in connection.execute("SELECT entity_type FROM ledger_audit_events")
        }

    assert {
        "provider",
        "account",
        "source_transaction",
        "transaction",
    }.issubset(entity_types)


def test_rule_then_history_classification(repository):
    repository.import_transactions(
        "file",
        "test",
        [transaction("one", "Kunde Rechnung 123"), transaction("two", "Kunde Rechnung 123")],
    )
    rows = repository.list_transactions()
    july = next(item for item in rows if item["reference"] == "one")
    august = next(item for item in rows if item["reference"] == "two")
    # The next identical purpose gets a previously confirmed classification.
    repository.classify(
        july["id"],
        "freelance",
        "income.freelance",
        "manual",
        1.0,
        "bestätigt",
        expected_version=july["version"],
    )
    result = ClassificationService(repository).classify(august["id"], "freelance")
    assert result is not None


def test_optimistic_write_protects_changes(repository):
    repository.import_transactions("file", "test", [transaction()])
    row = repository.list_transactions()[0]
    repository.classify(
        row["id"], "rental", "income.rent", "manual", 1.0, "first", expected_version=row["version"]
    )
    with pytest.raises(VersionConflictError):
        repository.classify(
            row["id"],
            "rental",
            "income.rent",
            "manual",
            1.0,
            "stale",
            expected_version=row["version"],
        )


def test_split_keeps_the_exact_amount_and_hides_parent(repository):
    repository.import_transactions("file", "test", [transaction(amount=Decimal("100.00"))])
    row = repository.list_transactions()[0]
    repository.split_transaction(
        row["id"],
        "rental",
        [
            (Decimal("70.00"), "income.rent", "rent"),
            (Decimal("30.00"), "income.utilities", "utilities"),
        ],
        row["version"],
    )
    active = repository.list_transactions()
    assert {item["amount_minor"] for item in active} == {7000, 3000}


def test_profile_scope_does_not_use_another_profiles_classification(repository):
    repository.import_transactions("file", "test", [transaction(purpose="Software")])
    row = repository.list_transactions()[0]
    repository.classify(
        row["id"],
        "freelance",
        "expense.business",
        "manual",
        1.0,
        "bestätigt",
        expected_version=row["version"],
    )

    assert repository.list_transactions()[0]["category_code"] == "expense.business"
    assert repository.list_transactions(profile_code="rental")[0]["category_code"] is None


def test_reports_include_more_than_500_rows(repository, tmp_path):
    repository.import_transactions(
        "file",
        "test",
        [transaction(str(index), amount=Decimal("1.00")) for index in range(501)],
    )

    assert len(LedgerReportGenerator(repository, tmp_path).dataframe()) == 501

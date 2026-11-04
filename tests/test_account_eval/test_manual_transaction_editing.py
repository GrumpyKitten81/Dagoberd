from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.repository import LedgerRepository, VersionConflictError


def test_manual_transaction_update_creates_source_revision(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="manual-edit-account"
    )
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("5"),
        purpose="Before",
        idempotency_key="manual-edit-transaction",
    )

    result = service.update_manual_transaction(
        transaction["id"],
        expected_version=transaction["version"],
        amount=Decimal("6"),
        purpose="After",
    )

    assert result["changed"] is True
    assert repository.get_transaction(transaction["id"])["purpose"] == "After"
    with repository.database.connect() as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM ledger_source_transaction_revisions
                   WHERE idempotency_key=?""",
                (f"manual-revision:{transaction['id']}:{result['version']}",),
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(VersionConflictError):
        service.update_manual_transaction(
            transaction["id"], expected_version=transaction["version"], purpose="Stale"
        )


def test_manual_transaction_update_rejects_bank_accounts_and_zero_amount(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    service = OrganizationService(repository)
    manual = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="manual-edit-bank-account"
    )
    transaction = service.create_manual_transaction(
        manual["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("5"),
        purpose="Cash",
        idempotency_key="manual-edit-bank-transaction",
    )
    with pytest.raises(ValueError, match="non-zero"):
        service.update_manual_transaction(
            transaction["id"], expected_version=transaction["version"], amount=Decimal("0")
        )

    repository.import_transactions(
        "bank",
        "Bank",
        [
            NormalizedTransaction(
                "bank-transaction",
                date(2026, 8, 1),
                Decimal("5"),
                "EUR",
                "Bank",
                "bank-account",
            )
        ],
    )
    with repository.database.connect() as connection:
        bank_transaction = dict(
            connection.execute(
                """SELECT t.* FROM ledger_transactions t
                   JOIN ledger_accounts a ON a.id=t.account_id
                   JOIN ledger_providers p ON p.id=a.provider_id
                   WHERE p.kind='bank'"""
            ).fetchone()
        )
    with pytest.raises(ValueError, match="only allowed on manual"):
        service.update_manual_transaction(
            bank_transaction["id"],
            expected_version=bank_transaction["version"],
            purpose="Still bank",
        )

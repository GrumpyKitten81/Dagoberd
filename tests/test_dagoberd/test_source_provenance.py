from datetime import date
from decimal import Decimal

from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction, TransactionSearchFilter
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository


def test_additional_source_identity_links_to_existing_transaction(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "manual",
        "Synthetic",
        [
            NormalizedTransaction(
                "canonical-1",
                date(2026, 1, 2),
                Decimal("-10.00"),
                "EUR",
                "Synthetic purchase",
                "cash",
            )
        ],
    )
    transaction = repository.list_transactions()[0]
    alias = NormalizedTransaction(
        "wiso-1",
        date(2026, 1, 3),
        Decimal("-10.00"),
        "EUR",
        "Imported purchase",
        "WISO cash",
        source_attributes={"contract": "synthetic-contract"},
        raw={"BuchungID": "wiso-1"},
    )

    created = repository.attach_source_alias(
        "file",
        "WISO Mein Geld 365",
        alias,
        transaction["id"],
        expected_transaction_version=transaction["version"],
    )
    repeated = repository.attach_source_alias(
        "file",
        "WISO Mein Geld 365",
        alias,
        transaction["id"],
        expected_transaction_version=transaction["version"],
    )

    assert created["created"] is True
    assert repeated["created"] is False
    detail = repository.transaction_detail(transaction["id"])
    assert len(detail["source_origins"]) == 2
    assert detail["source_attributes"]["contract"] == "synthetic-contract"
    results = OrganizationService(repository).search_transactions(
        TransactionSearchFilter(text="synthetic-contract")
    )
    assert results[0]["source_count"] == 2

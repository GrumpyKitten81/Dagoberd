from datetime import date
from decimal import Decimal

from account_eval.accounts import AccountScope
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.services.source_reconciliation import SourceReconciliationService


def test_source_reconciliation_preserves_origins_without_duplicate_cashflow(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "manual",
        "Current",
        [
            NormalizedTransaction(
                "target-one",
                date(2026, 1, 2),
                Decimal("-10"),
                "EUR",
                "Groceries",
                "target",
                valuta_date=date(2026, 1, 3),
            ),
            NormalizedTransaction(
                "target-two",
                date(2026, 1, 4),
                Decimal("-20"),
                "EUR",
                "Utilities",
                "target",
            ),
            NormalizedTransaction(
                "target-only",
                date(2026, 1, 5),
                Decimal("-30"),
                "EUR",
                "Target only",
                "target",
            ),
        ],
    )
    repository.import_transactions(
        "file",
        "Imported",
        [
            NormalizedTransaction(
                "source-one",
                date(2026, 1, 2),
                Decimal("-10"),
                "EUR",
                "Groceries",
                "source",
                valuta_date=date(2026, 1, 3),
            ),
            NormalizedTransaction(
                "source-two",
                date(2026, 1, 4),
                Decimal("-20"),
                "EUR",
                "Utilities",
                "source",
                valuta_date=date(2026, 1, 6),
            ),
            NormalizedTransaction(
                "source-only",
                date(2026, 1, 7),
                Decimal("-40"),
                "EUR",
                "Source only",
                "source",
            ),
        ],
    )
    accounts = {item["external_id"]: item for item in repository.list_accounts()}
    service = SourceReconciliationService(repository)

    preview = service.preview(accounts["source"]["id"], accounts["target"]["id"])
    result = service.run(accounts["source"]["id"], accounts["target"]["id"], preview.fingerprint)

    assert len(preview.mappings) == 2
    assert len(preview.unmatched_source) == 1
    assert preview.unmatched_target_count == 1
    assert result["moved_unmatched"] == 1
    target_transactions = repository.list_transactions(
        limit=None,
        account_scope=AccountScope.selected([accounts["target"]["id"]]),
    )
    assert len(target_transactions) == 4
    merged = next(item for item in target_transactions if item["purpose"] == "Groceries")
    assert len(repository.transaction_detail(merged["id"])["source_origins"]) == 2
    assert repository.get_account(accounts["source"]["id"])["enabled"] == 0
    assert repository.routed_account_id(accounts["source"]["id"]) == accounts["target"]["id"]

    repository.import_transactions(
        "file",
        "Imported",
        [
            NormalizedTransaction(
                "source-after-route",
                date(2026, 1, 8),
                Decimal("-50"),
                "EUR",
                "Routed import",
                "source",
            )
        ],
    )

    routed_transactions = repository.list_transactions(
        limit=None,
        account_scope=AccountScope.selected([accounts["target"]["id"]]),
    )
    assert {item["purpose"] for item in routed_transactions} >= {"Routed import"}

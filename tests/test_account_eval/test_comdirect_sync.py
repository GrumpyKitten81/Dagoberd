from datetime import date
from decimal import Decimal

import pytest

from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import AccountType, NormalizedTransaction, TransactionStatus
from account_eval.ledger.repository import LedgerRepository
from account_eval.providers.comdirect import (
    ComdirectProvider,
    ComdirectTemporaryError,
    TransactionPage,
)
from account_eval.services.comdirect_sync import ComdirectSyncService
from account_eval.services.source_reconciliation import SourceReconciliationService


def transaction(
    external_id: str,
    status: TransactionStatus = TransactionStatus.BOOKED,
    account_external_id: str = "remote-1",
) -> NormalizedTransaction:
    return NormalizedTransaction(
        external_id=external_id,
        account_external_id=account_external_id,
        booking_date=date(2026, 7, 1),
        amount=Decimal("10.00"),
        currency="EUR",
        purpose=external_id,
        raw={"id": external_id, "status": status.value},
        booking_status=status,
    )


class FakeProvider:
    def __init__(self, pages, *, accounts=None, fail_after=None):
        self.pages = pages
        self.accounts = accounts or [
            {
                "accountId": "remote-1",
                "accountDisplayId": "Comdirect Giro",
                "accountType": "GIRO",
                "balance": {"value": "123.45", "unit": "EUR"},
                "balanceDate": "2026-07-01",
            }
        ]
        self.fail_after = fail_after
        self.received_offsets = None
        self.received_accounts = None

    def list_accounts(self, *, interactive):
        assert interactive is False
        return self.accounts

    @staticmethod
    def account_type(account):
        return ComdirectProvider.account_type(account)

    def iter_transaction_pages(
        self,
        *,
        accounts,
        account_external_ids,
        booking_from,
        booking_to,
        start_offsets,
        interactive,
    ):
        self.received_offsets = dict(start_offsets)
        self.received_accounts = [account["accountId"] for account in accounts]
        for index, page in enumerate(self.pages):
            if page.account_external_id not in account_external_ids:
                continue
            if self.fail_after is not None and index >= self.fail_after:
                raise ComdirectTemporaryError("temporary")
            yield page


@pytest.fixture
def repository(tmp_path):
    return LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))


def test_comdirect_sync_is_idempotent_and_updates_balance(repository):
    page = TransactionPage("remote-1", 0, 1, (transaction("one"),))

    first = ComdirectSyncService(
        repository,
        FakeProvider([page]),
        "private",
    ).synchronize(interactive=False)
    second = ComdirectSyncService(
        repository,
        FakeProvider([page]),
        "private",
    ).synchronize(interactive=False)

    assert (first.imported, first.skipped, first.accounts) == (1, 0, 1)
    assert (second.imported, second.skipped, second.accounts) == (0, 1, 0)
    assert len(repository.list_transactions()) == 1
    assert repository.list_accounts()[0]["last_balance_minor"] == 12345
    assert len(repository.list_balance_snapshots()) == 1
    assert repository.list_balance_snapshots()[0]["balance_date"] == "2026-07-01"
    completed_run = repository.get_sync_run(second.sync_run_id)
    assert completed_run["status"] == "completed"
    assert completed_run["account_runs"][0]["status"] == "completed"
    assert completed_run["account_runs"][0]["skipped_count"] == 1


def test_comdirect_sync_routes_future_entries_to_reconciled_target_account(repository):
    repository.import_transactions(
        "file",
        "WISO",
        [
            NormalizedTransaction(
                "wiso-one",
                date(2026, 6, 1),
                Decimal("10.00"),
                "EUR",
                "WISO entry",
                "wiso-target",
            )
        ],
    )
    ComdirectSyncService(repository, FakeProvider([]), "private").synchronize(interactive=False)
    accounts = {account["external_id"]: account for account in repository.list_accounts()}
    source = accounts["remote-1"]
    target = accounts["wiso-target"]
    reconciliation = SourceReconciliationService(repository)
    preview = reconciliation.preview(source["id"], target["id"])
    reconciliation.run(source["id"], target["id"], preview.fingerprint)

    result = ComdirectSyncService(
        repository,
        FakeProvider([TransactionPage("remote-1", 0, 1, (transaction("after-route"),))]),
        "private",
    ).synchronize(interactive=False)

    routed = next(
        item for item in repository.list_transactions() if item["purpose"] == "after-route"
    )
    assert result.synchronized_accounts == 1
    assert routed["account_id"] == target["id"]
    assert repository.get_account(source["id"])["enabled"] == 0
    assert repository.get_account(target["id"])["last_balance_minor"] == 12345


def test_comdirect_sync_updates_pending_source_revision(repository):
    pending = TransactionPage(
        "remote-1",
        0,
        1,
        (transaction("one", TransactionStatus.PENDING),),
    )
    booked = TransactionPage(
        "remote-1",
        0,
        1,
        (transaction("one", TransactionStatus.BOOKED),),
    )

    ComdirectSyncService(repository, FakeProvider([pending]), "private").synchronize(
        interactive=False
    )
    result = ComdirectSyncService(repository, FakeProvider([booked]), "private").synchronize(
        interactive=False
    )

    assert result.updated == 1
    assert repository.list_transactions()[0]["booking_status"] == "booked"
    run = repository.get_sync_run(result.sync_run_id)
    assert run["updated_count"] == 1
    assert run["account_runs"][0]["updated_count"] == 1


def test_failed_comdirect_sync_can_resume_from_saved_cursor(repository):
    first_page = TransactionPage(
        "remote-1",
        0,
        1,
        (transaction("one"),),
    )
    failing_provider = FakeProvider(
        [
            first_page,
            TransactionPage(
                "remote-1",
                1,
                2,
                (transaction("two"),),
            ),
        ],
        fail_after=1,
    )

    with pytest.raises(ComdirectTemporaryError):
        ComdirectSyncService(
            repository,
            failing_provider,
            "private",
        ).synchronize(interactive=False)

    with repository.database.connect() as connection:
        failed_id = connection.execute(
            """SELECT id FROM ledger_sync_runs
               WHERE status='failed'"""
        ).fetchone()["id"]
    failed = repository.get_sync_run(failed_id)
    assert failed["cursor"] == {"remote-1": 1}
    assert failed["error_code"] == "temporary_error"
    assert failed["account_runs"][0]["status"] == "failed"
    assert failed["account_runs"][0]["cursor"] == 1

    resumed_provider = FakeProvider(
        [
            TransactionPage(
                "remote-1",
                1,
                2,
                (transaction("two"),),
            )
        ]
    )
    result = ComdirectSyncService(
        repository,
        resumed_provider,
        "private",
    ).synchronize(
        resume_sync_run_id=failed_id,
        interactive=False,
    )

    assert resumed_provider.received_offsets == {"remote-1": 1}
    assert result.imported == 1
    assert {row["purpose"] for row in repository.list_transactions()} == {
        "one",
        "two",
    }


def test_sync_separates_giro_and_tagesgeld_and_skips_other_products(repository):
    accounts = [
        {
            "accountId": "giro",
            "accountDisplayId": "Girokonto",
            "accountType": "GIRO",
            "balance": {"value": "123.45", "unit": "EUR"},
        },
        {
            "accountId": "savings",
            "accountDisplayId": "Tagesgeld",
            "accountType": "CALL_MONEY",
            "balance": {"value": "500.00", "unit": "EUR"},
        },
        {"accountId": "visa", "accountDisplayId": "Visa", "accountType": "CREDIT_CARD"},
        {"accountId": "depot", "accountDisplayId": "Depot", "accountType": "DEPOT"},
        {"accountId": "other", "accountDisplayId": "Other", "accountType": "UNKNOWN"},
    ]
    provider = FakeProvider(
        [
            TransactionPage("giro", 0, 1, (transaction("giro-1", account_external_id="giro"),)),
            TransactionPage(
                "savings",
                0,
                1,
                (transaction("savings-1", account_external_id="savings"),),
            ),
        ],
        accounts=accounts,
    )

    result = ComdirectSyncService(repository, provider, "private").synchronize(interactive=False)

    by_external_id = {account["external_id"]: account for account in repository.list_accounts()}
    assert set(by_external_id) == {"giro", "savings"}
    assert by_external_id["giro"]["account_type"] == AccountType.CHECKING.value
    assert by_external_id["savings"]["account_type"] == AccountType.SAVINGS.value
    assert provider.received_accounts == ["giro", "savings"]
    assert (result.discovered_accounts, result.synchronized_accounts, result.skipped_accounts) == (
        5,
        2,
        3,
    )

    savings = by_external_id["savings"]
    repository.update_account_alias(savings["id"], "Reserve", expected_version=savings["version"])
    repository.set_account_enabled(savings["id"], False, expected_version=savings["version"] + 1)

    ComdirectSyncService(repository, provider, "private").synchronize(interactive=False)

    persisted_savings = repository.get_account(savings["id"])
    assert persisted_savings["display_name"] == "Reserve"
    assert persisted_savings["enabled"] == 0


def test_sync_handles_nested_balance_account_payloads(repository):
    accounts = [
        {
            "accountId": "giro",
            "balance": {"value": "123.45", "unit": "EUR"},
            "account": {
                "accountId": "giro",
                "accountDisplayId": "Girokonto",
                "accountType": {"key": "CA", "text": "Girokonto"},
            },
        },
        {
            "accountId": "savings",
            "balance": {"value": "500.00", "unit": "EUR"},
            "account": {
                "accountId": "savings",
                "accountDisplayId": "Tagesgeld Plus",
                "accountType": {"key": "DAS", "text": "Tagesgeld"},
            },
        },
    ]
    provider = FakeProvider(
        [
            TransactionPage("giro", 0, 1, (transaction("giro-1", account_external_id="giro"),)),
            TransactionPage(
                "savings",
                0,
                1,
                (transaction("savings-1", account_external_id="savings"),),
            ),
        ],
        accounts=accounts,
    )

    result = ComdirectSyncService(repository, provider, "private").synchronize(interactive=False)

    by_external_id = {account["external_id"]: account for account in repository.list_accounts()}
    assert set(by_external_id) == {"giro", "savings"}
    assert by_external_id["giro"]["display_name"] == "Girokonto"
    assert by_external_id["savings"]["display_name"] == "Tagesgeld Plus"
    assert (result.discovered_accounts, result.synchronized_accounts, result.skipped_accounts) == (
        2,
        2,
        0,
    )


def test_named_connections_isolate_identical_external_account_ids(repository):
    private = FakeProvider([TransactionPage("remote-1", 0, 1, (transaction("private-entry"),))])
    shared = FakeProvider([TransactionPage("remote-1", 0, 1, (transaction("shared-entry"),))])

    ComdirectSyncService(repository, private, "private").synchronize(interactive=False)
    ComdirectSyncService(repository, shared, "shared").synchronize(interactive=False)

    accounts = repository.list_accounts()
    assert {account["provider_name"] for account in accounts} == {
        "Comdirect (private)",
        "Comdirect (shared)",
    }
    assert len(accounts) == 2
    assert {transaction["purpose"] for transaction in repository.list_transactions()} == {
        "private-entry",
        "shared-entry",
    }

    private_account = next(
        account for account in accounts if account["provider_name"] == "Comdirect (private)"
    )
    repository.set_account_enabled(
        private_account["id"],
        False,
        expected_version=private_account["version"],
    )
    result = ComdirectSyncService(repository, private, "private").synchronize(interactive=False)

    assert result.synchronized_accounts == 0

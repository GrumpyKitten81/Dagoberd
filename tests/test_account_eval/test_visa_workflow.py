"""End-to-end Visa file import and Giro settlement transfer workflow."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.ledger.semantics import TransactionSemanticsService
from account_eval.services.import_service import ImportService


@pytest.fixture
def repository(tmp_path: Path) -> LedgerRepository:
    return LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))


def booking(
    external_id: str,
    account: str,
    amount: str,
    *,
    booking_date: date = date(2026, 7, 31),
    purpose: str = "Visa",
) -> NormalizedTransaction:
    return NormalizedTransaction(
        external_id=external_id,
        account_external_id=account,
        booking_date=booking_date,
        amount=Decimal(amount),
        currency="EUR",
        purpose=purpose,
        reference=external_id,
        raw={"id": external_id},
    )


def test_visa_file_import_and_giro_settlement_transfer(
    repository: LedgerRepository,
) -> None:
    repo = repository
    giro_provider = repo.ensure_provider("comdirect", "Comdirect (private)")
    visa_provider = repo.ensure_provider("file", "VisaPrivat")
    giro_id, _ = repo.ensure_account(
        giro_provider,
        "giro-1",
        "Giro",
        "EUR",
        account_kind="checking",
    )
    visa_id, _ = repo.ensure_account(
        visa_provider,
        "VISA-1234",
        "Visa Privat",
        "EUR",
        account_kind="credit_card",
    )

    repo.import_transactions(
        "comdirect",
        "Comdirect (private)",
        [booking("giro-settlement", "giro-1", "-500.00", purpose="Visa Abrechnung")],
    )
    repo.import_transactions(
        "file",
        "VisaPrivat",
        [
            booking("visa-1", "VISA-1234", "-120.50", purpose="Supermarkt"),
            booking("visa-2", "VISA-1234", "-79.50", purpose="Tankstelle"),
            booking(
                "visa-payment",
                "VISA-1234",
                "500.00",
                purpose="Zahlung vom Giro",
            ),
        ],
    )

    visa_accounts = {account["id"]: account for account in repo.list_accounts()}
    assert visa_accounts[visa_id]["account_type"] == "credit_card"

    relations = TransactionSemanticsService(
        repo,
        transfer_max_days=14,
    ).suggest("transfer")
    assert len(relations) == 1
    relation = relations[0]

    giro_tx = next(
        item
        for item in repo.list_transactions(limit=None)
        if item["account_id"] == giro_id and item["amount_minor"] < 0
    )
    visa_tx = next(
        item
        for item in repo.list_transactions(limit=None)
        if item["account_id"] == visa_id and item["amount_minor"] > 0
    )
    assert {relation["source_transaction_id"], relation["target_transaction_id"]} == {
        giro_tx["id"],
        visa_tx["id"],
    }

    confirmed = repo.review_transaction_relation(
        relation["id"],
        "confirm",
        relation["version"],
    )
    assert confirmed["status"] == "confirmed"
    rows = repo.list_transactions(limit=None)
    transfer_rows = [item for item in rows if item["is_internal_transfer"]]
    assert len(transfer_rows) == 2
    assert sum(item["cashflow_amount_minor"] for item in rows) == -20000


def test_tabular_visa_profile_import(
    repository: LedgerRepository,
    tmp_path: Path,
) -> None:
    repo = repository
    provider_id = repo.ensure_provider("file", "VisaPrivat")
    account_id, _ = repo.ensure_account(
        provider_id,
        "VISA-5678",
        "Visa Business",
        "EUR",
        account_kind="credit_card",
    )
    path = tmp_path / "visa.csv"
    path.write_text(
        "Datum;Verwendungszweck;Betrag\n"
        "15.07.2026;Büromaterial;-42,50\n"
        "31.07.2026;Zahlung Giro;42,50\n",
        encoding="utf-8",
    )
    profiles = {
        "visa_comdirect": {
            "source": "tabular",
            "delimiter": ";",
            "encoding": "utf-8-sig",
            "default_currency": "EUR",
            "date_formats": ["%d.%m.%Y", "%Y-%m-%d"],
            "decimal_separator": ",",
            "mapping": {
                "booking_date": ["Datum", "Buchungsdatum"],
                "amount": ["Betrag", "Umsatz"],
                "purpose": ["Verwendungszweck", "Beschreibung"],
            },
        }
    }
    service = ImportService(repo, profiles=profiles)
    preview = service.preview("tabular", path, account_id=account_id, profile="visa_comdirect")
    assert preview.errors == ()
    assert preview.count == 2
    result = service.execute(preview.preview_id)
    assert result.imported == 2
    transactions = repo.list_transactions(
        limit=None,
        account_scope=None,
    )
    visa_rows = [item for item in transactions if item["account_id"] == account_id]
    assert len(visa_rows) == 2
    assert {item["amount_minor"] for item in visa_rows} == {-4250, 4250}

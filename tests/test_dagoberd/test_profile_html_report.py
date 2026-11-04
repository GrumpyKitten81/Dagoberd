from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.ledger_reports import LedgerReportGenerator


def _repository(tmp_path) -> LedgerRepository:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    return repository


def test_profile_html_report_is_collapsible_and_filterable(tmp_path):
    repository = _repository(tmp_path)
    organization = OrganizationService(repository)
    account_id = organization.create_manual_account(
        "Household cash",
        account_kind="cash",
        opening_balance=Decimal("100.00"),
        balance_date=date(2024, 1, 1),
        idempotency_key="household-html-report",
    )["id"]
    organization.create_manual_transaction(
        account_id,
        booking_date=date(2024, 3, 1),
        amount=Decimal("-12.50"),
        purpose="<script>alert(1)</script>",
        idempotency_key="household-html-tx-1",
        category_profile="household",
        category_code="expense.food",
        tags=("has-receipt",),
        dimensions={"person": "shared", "property": "property-1"},
    )

    path = LedgerReportGenerator(repository, tmp_path / "reports").generate(
        profile_code="household",
        formats=("html",),
    )[0]
    content = path.read_text(encoding="utf-8")

    assert "<h1>Household ledger</h1>" in content
    assert content.count("<details") >= 6
    assert " open" not in content
    assert 'class="filter-row"' in content
    assert "data-filter" in content
    assert "Person" in content
    assert "Property" in content
    assert "property-1" in content
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


def test_rental_html_report_uses_generic_profile_renderer(tmp_path):
    repository = _repository(tmp_path)
    repository.import_transactions(
        "file",
        "rental-export",
        [
            NormalizedTransaction(
                "rent-1",
                date(2026, 7, 1),
                Decimal("266"),
                "EUR",
                "Tenant payment",
                "account-one",
            )
        ],
    )
    transaction = repository.list_transactions(limit=1)[0]
    repository.classify(
        transaction["id"],
        "rental",
        "income.rent",
        "manual",
        1.0,
        "Test",
        expected_version=transaction["version"],
        metadata={"person_id": "Tenant"},
    )

    paths = LedgerReportGenerator(repository, tmp_path / "reports").generate(
        profile_code="rental",
        formats=("json", "html"),
    )
    content = next(path for path in paths if path.suffix == ".html").read_text(encoding="utf-8")
    payload = json.loads(
        next(path for path in paths if path.suffix == ".json").read_text(encoding="utf-8")
    )
    assert set(payload["categories"][0]) == {"Kategorie", "Währung", "Betrag"}
    assert set(payload["months"][0]) == {"Monat", "Währung", "Einnahmen", "Ausgaben", "Saldo"}
    assert "<th>Category</th>" in content
    assert "<th>Amount</th>" in content

    assert "<h1>Rental report</h1>" in content
    assert "Kautionsstatus WG" not in content
    assert "Miet-Monatsmatrix" not in content
    assert 'class="filter-row"' in content

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dagoberd.accounts import AccountScope
from dagoberd.config import ImportAnnotationConfig, ImportProfileConfig
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService
from dagoberd.services.import_annotations import ImportAnnotationService
from dagoberd.services.import_service import ImportService


def _repository(tmp_path: Path) -> LedgerRepository:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    return repository


def _manual_cash_account(repository: LedgerRepository) -> str:
    organization = OrganizationService(repository)
    created = organization.create_manual_account(
        "Haushaltskasse",
        account_kind="cash",
        opening_balance=Decimal("100.00"),
        balance_date=date(2024, 1, 1),
        idempotency_key="household-cash-account-test",
    )
    return created["id"]


def test_import_annotations_map_category_dimensions_and_tags(tmp_path):
    repository = _repository(tmp_path)
    config = ImportAnnotationConfig(
        ignore_values=("Keine",),
        category_column="Kategorie",
        category_profile="household",
        category_aliases={"Kinder": "expense.children"},
        dimensions={"Frei 1": "person"},
        dimension_aliases={"Shared": "shared"},
        profile_columns={"Steuer": "spain-tax"},
        profile_category_aliases={
            "spain-tax": {"Einkünfte aus Vermietung und Verpachtung": "income.rental"}
        },
        tag_if_positive={"Dokumente": "has-receipt"},
    )
    transaction = NormalizedTransaction(
        external_id="ext-1",
        booking_date=date(2024, 1, 2),
        amount=Decimal("-12.50"),
        currency="EUR",
        purpose="Babysitter",
        account_external_id="cash-1",
        raw={
            "Kategorie": "Kinder",
            "Frei 1": "Shared",
            "Steuer": "Einkünfte aus Vermietung und Verpachtung",
            "Dokumente": "1",
        },
    )
    plans = ImportAnnotationService(repository).plan([transaction], config)
    assert plans[0].categories == {
        "household": "expense.children",
        "spain-tax": "income.rental",
    }
    assert plans[0].dimensions == {"person": "shared"}
    assert plans[0].tags == ("has-receipt",)


def test_import_annotations_warn_on_unknown_dimension_value(tmp_path):
    repository = _repository(tmp_path)
    config = ImportAnnotationConfig(
        dimensions={"Frei 3": "jurisdiction"},
        dimension_ignore_values=("Rechnung", "Nebenkosten"),
    )
    transaction = NormalizedTransaction(
        external_id="ext-2",
        booking_date=date(2024, 1, 3),
        amount=Decimal("5"),
        currency="EUR",
        purpose="Test",
        account_external_id="cash-1",
        raw={"Frei 3": "Rechnung"},
    )
    plans = ImportAnnotationService(repository).plan([transaction], config)
    assert plans[0].dimensions == {}
    assert not plans[0].warnings

    unknown = NormalizedTransaction(
        external_id="ext-3",
        booking_date=date(2024, 1, 4),
        amount=Decimal("5"),
        currency="EUR",
        purpose="Test",
        account_external_id="cash-1",
        raw={"Frei 3": "Unbekannt"},
    )
    plans = ImportAnnotationService(repository).plan([unknown], config)
    assert plans[0].warnings


def test_tabular_import_allows_manual_account(tmp_path):
    repository = _repository(tmp_path)
    account_id = _manual_cash_account(repository)
    csv_path = tmp_path / "cash.csv"
    csv_path.write_text(
        "Wertstellung;Verwendungszweck;Betrag\n2024-02-01;Bar Ausgabe;-10.00\n",
        encoding="utf-8",
    )
    profile = ImportProfileConfig(
        source="tabular",
        mapping={
            "booking_date": ("Wertstellung",),
            "amount": ("Betrag",),
            "purpose": ("Verwendungszweck",),
        },
        delimiter=";",
    )
    service = ImportService(repository, profiles={"cash": profile})
    preview = service.preview("tabular", csv_path, account_id=account_id, profile="cash")
    assert preview.count == 1
    result = service.execute(preview.preview_id)
    assert result.imported == 1


def test_tabular_import_rejects_comdirect_account(tmp_path):
    repository = _repository(tmp_path)
    provider_id = repository.ensure_provider("comdirect", "Comdirect")
    with repository.database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_accounts(
                   id, provider_id, external_id, display_name, currency, account_type
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            ("account-comdirect", provider_id, "123", "Giro", "EUR", "checking"),
        )
    csv_path = tmp_path / "cash.csv"
    csv_path.write_text(
        "Wertstellung;Verwendungszweck;Betrag\n2024-02-01;Test;-1.00\n",
        encoding="utf-8",
    )
    profile = ImportProfileConfig(
        source="tabular",
        mapping={
            "booking_date": ("Wertstellung",),
            "amount": ("Betrag",),
            "purpose": ("Verwendungszweck",),
        },
        delimiter=";",
    )
    service = ImportService(repository, profiles={"cash": profile})
    with pytest.raises(ValueError, match="expected file"):
        service.preview("tabular", csv_path, account_id="account-comdirect", profile="cash")


def test_skip_opening_balance_row(tmp_path):
    repository = _repository(tmp_path)
    account_id = _manual_cash_account(repository)
    csv_path = tmp_path / "cash.csv"
    csv_path.write_text(
        "Wertstellung;Verwendungszweck;Betrag\n"
        "2024-01-01;Eröffnungssaldo Start;100.00\n"
        "2024-02-01;Einkauf;-5.00\n",
        encoding="utf-8",
    )
    profile = ImportProfileConfig(
        source="tabular",
        mapping={
            "booking_date": ("Wertstellung",),
            "amount": ("Betrag",),
            "purpose": ("Verwendungszweck",),
        },
        delimiter=";",
        annotations=ImportAnnotationConfig(skip_purpose_prefixes=("Eröffnungssaldo",)),
    )
    service = ImportService(repository, profiles={"cash": profile})
    preview = service.preview("tabular", csv_path, account_id=account_id, profile="cash")
    assert preview.count == 1


def test_analysis_group_by_dimension(tmp_path):
    repository = _repository(tmp_path)
    organization = OrganizationService(repository)
    account_id = _manual_cash_account(repository)
    organization.create_manual_transaction(
        account_id,
        booking_date=date(2024, 3, 1),
        amount=Decimal("-20"),
        purpose="Ausgabe A",
        idempotency_key="household-analysis-a",
        category_profile="household",
        category_code="expense.household",
        dimensions={"person": "shared"},
    )
    organization.create_manual_transaction(
        account_id,
        booking_date=date(2024, 3, 2),
        amount=Decimal("-30"),
        purpose="Ausgabe B",
        idempotency_key="household-analysis-b",
        category_profile="household",
        category_code="expense.children",
        dimensions={"person": "i"},
    )
    service = AnalysisService(repository)
    definition = AnalysisDefinition(
        account_scope=AccountScope.selected([account_id]),
        profile="household",
        booking_from="2024-03-01",
        booking_to="2024-03-31",
        group_by=("dimension:person",),
        sections=("income_expense",),
        formats=("json",),
    )
    result = service.analyze(definition)
    groups = {item["dimension_person"]: item for item in result["groupings"]["dimension:person"]}
    assert groups["shared"]["transaction_count"] == 1
    assert groups["i"]["transaction_count"] == 1


def test_manual_add_applies_category_and_dimension(tmp_path):
    repository = _repository(tmp_path)
    organization = OrganizationService(repository)
    account_id = _manual_cash_account(repository)
    created = organization.create_manual_transaction(
        account_id,
        booking_date=date(2024, 4, 1),
        amount=Decimal("-8.50"),
        purpose="Snack",
        idempotency_key="manual-snack-1",
        category_profile="household",
        category_code="expense.food",
        tags=("bar",),
        dimensions={"person": "a"},
    )
    rows = repository.list_transactions(
        limit=10,
        account_scope=AccountScope.selected([account_id]),
    )
    row = next(item for item in rows if item["id"] == created["id"])
    assert row["category_code"] == "expense.food"
    assert "bar" in row["tags"]
    assert row["dimensions"]["person"] == "a"


def test_household_profile_is_installed(tmp_path):
    repository = _repository(tmp_path)
    with repository.database.connect() as connection:
        row = connection.execute(
            """SELECT c.code FROM ledger_categories c
               JOIN ledger_profile_categories pc ON pc.category_id=c.id
               JOIN ledger_profiles p ON p.id=pc.profile_id
               WHERE p.code='household' AND c.code='expense.leisure'"""
        ).fetchone()
    assert row is not None

from __future__ import annotations

import pytest

from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.profiles import install_builtin_profiles


def test_account_field_options_limit_values_per_dimension(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="field-option-account"
    )
    service.create_tag("Keep")
    service.create_tag("Drop")
    service.create_dimension_value("household", "one", "One")
    service.create_dimension_value("household", "two", "Two")
    service.create_dimension("test-dimension", "Test dimension")
    service.create_dimension_value("test-dimension", "three", "Three")

    service.set_account_field_options(account["id"], "tag", ["Keep"])
    service.set_account_field_options(account["id"], "dimension_value", ["household:one"])
    view = service.account_field_options_view(account["id"])

    assert view["tag"] == ["Keep"]
    assert view["dimension_value"]["household"] == [{"code": "one", "name": "One"}]
    assert view["dimension_value"]["test-dimension"] == [{"code": "three", "name": "Three"}]
    assert any(
        category["code"] == "expense.children" and category["name"] == "Children"
        for category in view["category"]["household"]
    )
    with pytest.raises(KeyError):
        service.set_account_field_options(account["id"], "tag", ["Missing"])


def test_cleared_account_field_options_offer_every_value(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="field-option-reset"
    )
    service.create_dimension_value("household", "one", "One")
    service.create_dimension_value("household", "two", "Two")
    service.set_account_field_options(account["id"], "dimension_value", ["household:one"])

    service.set_account_field_options(account["id"], "dimension_value", [])

    assert (
        len(service.account_field_options_view(account["id"])["dimension_value"]["household"]) == 2
    )

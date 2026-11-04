from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.repository import LedgerRepository
from account_eval.profiles import install_builtin_profiles


def test_dimension_and_value_lifecycle_requires_no_usage(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="dimension-lifecycle-account"
    )
    dimension = service.create_dimension("project-code", "Project code")
    value = service.create_dimension_value("project-code", "a", "Project A")
    renamed = service.update_dimension(
        "project-code", name="Project", expected_version=dimension["version"]
    )
    assert renamed["name"] == "Project"
    deactivated = service.update_dimension_value(
        "project-code", "a", active=False, expected_version=value["version"]
    )
    assert deactivated["active"] == 0
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-1"),
        purpose="Used dimension",
        idempotency_key="dimension-lifecycle-transaction",
    )
    service.set_transaction_dimension(
        transaction["id"], "project-code", "a", expected_version=transaction["version"]
    )
    with pytest.raises(ValueError, match="used by 1 transaction"):
        service.delete_dimension_value("project-code", "a")
    with pytest.raises(ValueError, match="used by 1 transaction"):
        service.delete_dimension("project-code")

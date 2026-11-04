import json
from datetime import date
from decimal import Decimal

import pytest

from dagoberd.accounts import AccountScope
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.repository import (
    LedgerRepository,
    VersionConflictError,
)


@pytest.fixture
def repository(tmp_path):
    return LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))


def transaction(external_id: str, account: str) -> NormalizedTransaction:
    return NormalizedTransaction(
        external_id=external_id,
        booking_date=date(2026, 7, 1),
        amount=Decimal("10.00"),
        currency="EUR",
        purpose=external_id,
        account_external_id=account,
    )


def test_account_scope_filters_selected_and_all_enabled_accounts(repository):
    repository.import_transactions(
        "file",
        "test",
        [transaction("one", "a"), transaction("two", "b")],
    )
    accounts = {account["external_id"]: account for account in repository.list_accounts()}

    selected = repository.list_transactions(
        account_scope=AccountScope.selected([accounts["a"]["id"]])
    )
    assert [row["purpose"] for row in selected] == ["one"]

    repository.set_account_enabled(
        accounts["b"]["id"],
        False,
        expected_version=accounts["b"]["version"],
    )
    all_enabled = repository.list_transactions(account_scope=AccountScope.all())
    assert [row["purpose"] for row in all_enabled] == ["one"]


def test_account_alias_is_version_protected_and_audited(repository):
    provider_id = repository.ensure_provider("file", "test")
    account_id, _ = repository.ensure_account(
        provider_id,
        "external",
        "Before",
        "EUR",
    )
    account = repository.get_account(account_id)

    assert (
        repository.update_account_alias(
            account_id,
            "After",
            expected_version=account["version"],
        )
        == 2
    )
    with pytest.raises(VersionConflictError):
        repository.update_account_alias(
            account_id,
            "Stale",
            expected_version=account["version"],
        )
    assert repository.get_account(account_id)["display_name"] == "After"


def test_account_groups_contain_explicit_active_accounts(repository):
    provider_id = repository.ensure_provider("file", "test")
    first, _ = repository.ensure_account(
        provider_id,
        "one",
        "One",
        "EUR",
    )
    second, _ = repository.ensure_account(
        provider_id,
        "two",
        "Two",
        "EUR",
    )

    identifier = repository.create_account_group(
        "Selected",
        [first, second],
        "Two accounts",
    )

    groups = repository.list_account_groups()
    assert len(groups) == 1
    assert groups[0]["id"] == identifier
    assert groups[0]["name"] == "Selected"
    assert groups[0]["description"] == "Two accounts"
    assert groups[0]["account_ids"] == sorted([first, second])
    assert repository.account_scope_for_group("Selected") == (
        AccountScope.selected(sorted([first, second]))
    )


def test_account_group_members_are_version_protected_and_audited(repository):
    provider_id = repository.ensure_provider("file", "test")
    first, _ = repository.ensure_account(provider_id, "one", "One", "EUR")
    second, _ = repository.ensure_account(provider_id, "two", "Two", "EUR")
    third, _ = repository.ensure_account(provider_id, "three", "Three", "EUR")
    group_id = repository.create_account_group("Selected", [first, second])

    version = repository.update_account_group_members(
        "Selected",
        [second, third],
        expected_version=1,
    )

    group = repository.list_account_groups()[0]
    assert version == 2
    assert group["version"] == 2
    assert group["account_ids"] == sorted([second, third])
    with repository.database.connect() as connection:
        audit = connection.execute(
            """SELECT operation, before_json, after_json
               FROM ledger_audit_events
               WHERE entity_type='account_group' AND entity_id=?
                 AND operation='update_members'""",
            (group_id,),
        ).fetchone()
    assert audit["operation"] == "update_members"
    assert json.loads(audit["before_json"]) == {
        "account_ids": sorted([first, second]),
        "version": 1,
    }
    assert json.loads(audit["after_json"]) == {
        "account_ids": sorted([second, third]),
        "version": 2,
    }

    assert (
        repository.update_account_group_members(
            group_id,
            [third, second],
            expected_version=2,
        )
        == 2
    )
    with repository.database.connect() as connection:
        update_count = connection.execute(
            """SELECT count(*) FROM ledger_audit_events
               WHERE entity_type='account_group' AND entity_id=?
                 AND operation='update_members'""",
            (group_id,),
        ).fetchone()[0]
    assert update_count == 1

    with pytest.raises(VersionConflictError):
        repository.update_account_group_members(
            group_id,
            [first],
            expected_version=1,
        )


def test_account_group_update_rejects_disabled_accounts(repository):
    provider_id = repository.ensure_provider("file", "test")
    enabled_id, _ = repository.ensure_account(provider_id, "one", "One", "EUR")
    disabled_id, _ = repository.ensure_account(provider_id, "two", "Two", "EUR")
    repository.create_account_group("Selected", [enabled_id])
    disabled = repository.get_account(disabled_id)
    repository.set_account_enabled(
        disabled_id,
        False,
        expected_version=disabled["version"],
    )

    with pytest.raises(ValueError, match="Disabled"):
        repository.update_account_group_members(
            "Selected",
            [disabled_id],
            expected_version=1,
        )


def test_register_file_account_with_credit_card_type(repository):
    provider_id = repository.ensure_provider("file", "VisaPrivat")
    account_id, created = repository.ensure_account(
        provider_id,
        "VISA-1234",
        "Visa Privat",
        "EUR",
        account_kind="credit_card",
    )
    assert created is True
    account = repository.get_account(account_id)
    assert account["account_type"] == "credit_card"
    assert account["provider_kind"] == "file"


def test_account_scope_rejects_duplicates_and_disabled_selection(repository):
    with pytest.raises(ValueError, match="unique"):
        AccountScope.selected(["same", "same"])

    provider_id = repository.ensure_provider("file", "test")
    account_id, _ = repository.ensure_account(
        provider_id,
        "external",
        "Account",
        "EUR",
    )
    account = repository.get_account(account_id)
    repository.set_account_enabled(
        account_id,
        False,
        expected_version=account["version"],
    )

    with pytest.raises(ValueError, match="Disabled"):
        repository.resolve_account_scope(AccountScope.selected([account_id]))

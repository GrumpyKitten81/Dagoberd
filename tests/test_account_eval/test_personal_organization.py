from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from account_eval.accounts import AccountScope
from account_eval.cli import main
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction, TransactionSearchFilter
from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.repository import LedgerRepository, VersionConflictError
from account_eval.mcp_server import TOOL_DEFINITIONS, MCPService
from account_eval.profiles import install_builtin_profiles


def _service(tmp_path) -> tuple[LedgerRepository, OrganizationService]:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    return repository, OrganizationService(repository)


def _minimal_config(tmp_path) -> Path:
    config = tmp_path / "account-eval.toml"
    config.write_text(
        "[database]\npath='output/account-eval.db'\n"
        "[reporting]\noutput_dir='output'\n"
        "[documents]\nstorage_dir='output/documents'\n"
        "[comdirect.connections.private]\nenabled=false\n",
        encoding="utf-8",
    )
    return config


def test_manual_accounts_assets_and_transactions_are_idempotent(tmp_path):
    repository, service = _service(tmp_path)

    account = service.create_manual_account(
        "Tagesgeld",
        account_kind="asset",
        currency="EUR",
        opening_balance=Decimal("1234.56"),
        balance_date=date(2026, 7, 1),
        idempotency_key="account-asset-001",
    )
    repeated = service.create_manual_account(
        "Tagesgeld",
        account_kind="asset",
        currency="EUR",
        opening_balance=Decimal("1234.56"),
        balance_date=date(2026, 7, 1),
        idempotency_key="account-asset-001",
    )
    assert account["created"] is True
    assert repeated["id"] == account["id"]
    assert repeated["created"] is False
    assert account["account_type"] == "asset"
    assert repository.list_balance_snapshots(account_id=account["id"])[0]["balance_minor"] == 123456
    with pytest.raises(ValueError, match="different account data"):
        service.create_manual_account(
            "Anderer Name",
            account_kind="asset",
            idempotency_key="account-asset-001",
            opening_balance=Decimal("1234.56"),
        )

    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 2),
        amount=Decimal("10.25"),
        purpose="Manuelle Zinsgutschrift",
        idempotency_key="manual-booking-001",
    )
    repeated_transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 2),
        amount=Decimal("10.25"),
        purpose="Manuelle Zinsgutschrift",
        idempotency_key="manual-booking-001",
    )
    assert transaction["created"] is True
    assert repeated_transaction["id"] == transaction["id"]
    assert repeated_transaction["created"] is False
    assert len(repository.list_transactions()) == 1
    reviews = service.list_review_items(account_scope=AccountScope.selected([account["id"]]))
    assert any(item["entity_id"] == transaction["id"] for item in reviews)


def test_tags_dimensions_merchants_and_composable_search(tmp_path):
    _repository, service = _service(tmp_path)
    account = service.create_manual_account(
        "Bargeld", account_kind="cash", idempotency_key="cash-account-01"
    )
    groceries = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 3),
        amount=Decimal("-42.00"),
        purpose="SUPERMARKT 4711",
        counterparty="Super Markt GmbH",
        idempotency_key="cash-booking-01",
    )
    other = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 4),
        amount=Decimal("-5.00"),
        purpose="Kaffee",
        idempotency_key="cash-booking-02",
    )

    service.create_tag("Lebensmittel")
    tagged = service.tag_transaction(
        groceries["id"], "Lebensmittel", expected_version=groceries["version"]
    )
    service.create_dimension_value("household", "family", "Familie")
    dimensioned = service.set_transaction_dimension(
        groceries["id"],
        "household",
        "family",
        expected_version=tagged["version"],
    )
    merchant = service.create_merchant("Supermarkt")
    service.add_merchant_alias(merchant["id"], "Super Markt GmbH")
    merchant_set = service.set_transaction_merchant(
        groceries["id"],
        merchant["id"],
        expected_version=dimensioned["version"],
    )

    results = service.search_transactions(
        TransactionSearchFilter(
            text="super",
            tags=("Lebensmittel",),
            dimensions={"household": ("family",)},
            merchant="Supermarkt",
            amount_max_minor=-100,
            only_unreviewed=True,
        ),
        account_scope=AccountScope.selected([account["id"]]),
    )
    assert [item["id"] for item in results] == [groceries["id"]]
    assert results[0]["tags"] == ["Lebensmittel"]
    assert results[0]["dimensions"] == {"household": "family"}
    assert results[0]["merchant"] == "Supermarkt"
    assert merchant_set["version"] == groceries["version"] + 3
    assert other["id"] not in {item["id"] for item in results}


def test_merchant_aliases_are_applied_to_later_imports(tmp_path):
    repository, service = _service(tmp_path)
    merchant = service.create_merchant("ACME")
    service.add_merchant_alias(merchant["id"], "Acme Payments S.A.")

    repository.import_transactions(
        "file",
        "Test",
        [
            NormalizedTransaction(
                "booking-1",
                date(2026, 7, 5),
                Decimal("-19.99"),
                "EUR",
                "Kartenzahlung",
                "account-1",
                counterparty="ACME PAYMENTS S.A.",
            )
        ],
    )

    result = service.search_transactions(TransactionSearchFilter(merchant="ACME"))[0]
    assert result["merchant"] == "ACME"


def test_review_inbox_is_versioned_and_tracks_dynamic_suggestions(tmp_path):
    _repository, service = _service(tmp_path)
    account = service.create_manual_account(
        "Bar", account_kind="cash", idempotency_key="review-account"
    )
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 6),
        amount=Decimal("-2"),
        purpose="Unklar",
        booking_status="pending",
        idempotency_key="review-booking",
    )
    open_items = service.list_review_items(account_scope=AccountScope.selected([account["id"]]))
    kinds = {item["item_type"] for item in open_items if item["entity_id"] == transaction["id"]}
    assert kinds == {"new_transaction", "pending_transaction"}

    new_item = next(item for item in open_items if item["item_type"] == "new_transaction")
    result = service.review_item(new_item["id"], "resolve", expected_version=new_item["version"])
    assert result["status"] == "resolved"
    with pytest.raises(VersionConflictError):
        service.review_item(new_item["id"], "dismiss", expected_version=new_item["version"])


def test_import_warnings_duplicates_and_errors_enter_review_inbox(tmp_path):
    repository, service = _service(tmp_path)
    preview_id = repository.create_import_preview(
        source="tabular",
        file_path="/local/test.csv",
        file_hash="hash",
        account_id=None,
        mapper_version="test",
        summary={
            "duplicate_candidates": 2,
            "warnings": ["Spaltenzuordnung prüfen"],
        },
    )
    repository.fail_import_preview(preview_id, "InvalidRow")

    items = service.list_review_items()
    relevant = {item["item_type"] for item in items if item["entity_id"] == preview_id}
    assert relevant == {"duplicate_candidate", "import_warning", "processing_error"}


def test_bulk_preview_is_atomic_versioned_and_audited(tmp_path):
    repository, service = _service(tmp_path)
    account = service.create_manual_account(
        "Bar", account_kind="cash", idempotency_key="bulk-account"
    )
    first = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 1),
        amount=Decimal("-10"),
        purpose="Software A",
        idempotency_key="bulk-booking-a",
    )
    second = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 7, 2),
        amount=Decimal("-20"),
        purpose="Software B",
        idempotency_key="bulk-booking-b",
    )
    preview = service.create_bulk_preview(
        "categorize",
        [first["id"], second["id"]],
        {"profile": "freelance", "category": "expense.business"},
    )
    service.create_tag("changed-after-preview")
    service.tag_transaction(first["id"], "changed-after-preview", expected_version=first["version"])
    with pytest.raises(VersionConflictError, match="stale"):
        service.execute_bulk_preview(preview["preview_id"])
    assert repository.list_classifications(status="applied") == []

    refreshed = service.create_bulk_preview(
        "categorize",
        [first["id"], second["id"]],
        {"profile": "freelance", "category": "expense.business"},
    )
    result = service.execute_bulk_preview(refreshed["preview_id"])
    assert result["changed"] == 2
    assert len(repository.list_classifications(status="applied")) == 2
    repeated = service.execute_bulk_preview(refreshed["preview_id"])
    assert repeated["already_executed"] is True
    with repository.database.connect() as connection:
        correlations = connection.execute(
            """SELECT count(*) FROM ledger_audit_events
               WHERE correlation_id=?""",
            (refreshed["preview_id"],),
        ).fetchone()[0]
    assert correlations == 3


def test_composite_bulk_edit_uses_common_options_and_touches_each_transaction_once(tmp_path):
    repository, service = _service(tmp_path)
    first_account = service.create_manual_account(
        "First", account_kind="cash", idempotency_key="bulk-edit-account-first"
    )
    second_account = service.create_manual_account(
        "Second", account_kind="cash", idempotency_key="bulk-edit-account-second"
    )
    service.create_tag("Shared")
    service.create_dimension_value("person", "person-b", "Person B")
    merchant = service.create_merchant("Common merchant")
    first = service.create_manual_transaction(
        first_account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-10"),
        purpose="First booking",
        idempotency_key="bulk-edit-booking-first",
    )
    second = service.create_manual_transaction(
        second_account["id"],
        booking_date=date(2026, 8, 2),
        amount=Decimal("-20"),
        purpose="Second booking",
        idempotency_key="bulk-edit-booking-second",
    )
    first_note = service.set_transaction_note(
        first["id"], "Existing", expected_version=first["version"]
    )
    versions = {
        first["id"]: first_note["version"],
        second["id"]: second["version"],
    }

    context = service.bulk_edit_context([first["id"], second["id"]])
    assert context["current"]["note"]["state"] == "mixed"
    assert context["options"]["statuses"] == ["corrected", "reversed"]
    assert context["amount_totals"] == [{"currency": "EUR", "amount_minor": -3000}]

    preview = service.create_bulk_preview(
        "edit_fields",
        [first["id"], second["id"]],
        {
            "category": {"profile": "freelance", "category": "expense.business"},
            "tags": {"add": ["Shared"], "remove": []},
            "merchant": {"mode": "set", "value": merchant["canonical_name"]},
            "dimensions": {"person": {"mode": "set", "value": "person-b"}},
            "note": {"mode": "append", "text": "Reviewed together"},
            "status": {"value": "corrected"},
        },
    )
    assert preview["changed_transaction_count"] == 2
    assert {change["field"] for change in preview["field_changes"]} == {
        "category",
        "dimension:person",
        "merchant",
        "note",
        "status",
        "tags",
    }
    with pytest.raises(ValueError, match="require the preview fingerprint"):
        service.execute_bulk_preview(preview["preview_id"])

    result = service.execute_bulk_preview(
        preview["preview_id"], fingerprint=preview["version_fingerprint"]
    )
    assert result["changed"] == 2
    assert len(result["updated_rows"]) == 2
    for transaction_id in (first["id"], second["id"]):
        detail = repository.transaction_detail(transaction_id)
        assert detail["version"] == versions[transaction_id] + 1
        assert detail["merchant_id"] == merchant["id"]
        assert detail["tags"] == ["Shared"]
        assert detail["dimensions"] == {"person": "person-b"}
        assert detail["booking_status"] == "corrected"
        assert detail["note"].endswith("Reviewed together")
        assert detail["status_history"][-1]["transaction_version"] == detail["version"]
    assert repository.transaction_detail(first["id"])["note"] == "Existing\nReviewed together"

    repeated = service.execute_bulk_preview(
        preview["preview_id"], fingerprint=preview["version_fingerprint"]
    )
    assert repeated["already_executed"] is True
    assert repeated["changed"] == 0

    clear_preview = service.create_bulk_preview(
        "edit_fields",
        [first["id"], second["id"]],
        {
            "tags": {"add": [], "remove": ["Shared"]},
            "merchant": {"mode": "clear"},
            "dimensions": {"person": {"mode": "clear"}},
            "note": {"mode": "clear"},
        },
    )
    cleared = service.execute_bulk_preview(
        clear_preview["preview_id"],
        fingerprint=clear_preview["version_fingerprint"],
    )
    assert cleared["changed"] == 2
    for transaction_id in (first["id"], second["id"]):
        detail = repository.transaction_detail(transaction_id)
        assert detail["merchant_id"] is None
        assert detail["tags"] == []
        assert detail["dimensions"] == {}
        assert detail["note"] == ""


def test_bulk_edit_context_intersects_account_whitelists(tmp_path):
    _repository, service = _service(tmp_path)
    first_account = service.create_manual_account(
        "First", account_kind="cash", idempotency_key="bulk-options-first"
    )
    second_account = service.create_manual_account(
        "Second", account_kind="cash", idempotency_key="bulk-options-second"
    )
    service.create_tag("Shared")
    service.create_tag("First only")
    service.set_account_field_options(first_account["id"], "tag", ["Shared", "First only"])
    service.set_account_field_options(second_account["id"], "tag", ["Shared"])
    service.set_account_field_options(
        first_account["id"], "category", ["freelance:expense.business"]
    )
    service.set_account_field_options(
        second_account["id"], "category", ["freelance:expense.business"]
    )
    first = service.create_manual_transaction(
        first_account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-1"),
        purpose="First",
        idempotency_key="bulk-options-booking-first",
    )
    second = service.create_manual_transaction(
        second_account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-1"),
        purpose="Second",
        idempotency_key="bulk-options-booking-second",
    )

    context = service.bulk_edit_context([first["id"], second["id"]])

    assert context["options"]["tags"] == ["Shared"]
    assert [item["code"] for item in context["options"]["category"]["freelance"]] == [
        "expense.business"
    ]
    with pytest.raises(ValueError, match="not allowed for every selected account"):
        service.create_bulk_preview(
            "edit_fields",
            [first["id"], second["id"]],
            {"tags": {"add": ["First only"], "remove": []}},
        )


def test_composite_bulk_edit_validates_note_length_and_tag_conflicts(tmp_path):
    _repository, service = _service(tmp_path)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="bulk-validation-account"
    )
    service.create_tag("Review")
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-1"),
        purpose="Booking",
        idempotency_key="bulk-validation-booking",
    )
    service.set_transaction_note(
        transaction["id"], "x" * 1999, expected_version=transaction["version"]
    )

    with pytest.raises(ValueError, match="must not exceed 2000"):
        service.create_bulk_preview(
            "edit_fields",
            [transaction["id"]],
            {"note": {"mode": "append", "text": "too long"}},
        )
    with pytest.raises(ValueError, match="cannot be added and removed"):
        service.create_bulk_preview(
            "edit_fields",
            [transaction["id"]],
            {"tags": {"add": ["Review"], "remove": ["Review"]}},
        )
    with pytest.raises(ValueError, match="limited to 500"):
        service.bulk_edit_context(str(index) for index in range(501))


def test_bulk_edit_disables_status_for_split_parents_but_keeps_other_fields(tmp_path):
    repository, service = _service(tmp_path)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="bulk-split-account"
    )
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-10"),
        purpose="Split booking",
        idempotency_key="bulk-split-booking",
    )
    repository.split_transaction(
        transaction["id"],
        "freelance",
        [
            (Decimal("-4"), "expense.business", "First"),
            (Decimal("-6"), "expense.business", "Second"),
        ],
        transaction["version"],
    )

    context = service.bulk_edit_context([transaction["id"]])

    assert context["options"]["statuses"] == []
    assert context["disabled"]["status"]
    with pytest.raises(ValueError, match="Status transition"):
        service.create_bulk_preview(
            "edit_fields",
            [transaction["id"]],
            {"status": {"value": "corrected"}},
        )
    preview = service.create_bulk_preview(
        "edit_fields",
        [transaction["id"]],
        {"note": {"mode": "replace", "text": "Split reviewed"}},
    )
    assert preview["changed_transaction_count"] == 1


def test_cli_exposes_composite_bulk_preview_and_fingerprint_run(tmp_path, capsys):
    config = _minimal_config(tmp_path)
    database_path = tmp_path / "bulk-cli.db"
    repository = LedgerRepository(LedgerDatabase(database_path))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="bulk-cli-account"
    )
    transaction = service.create_manual_transaction(
        account["id"],
        booking_date=date(2026, 8, 1),
        amount=Decimal("-1"),
        purpose="CLI booking",
        idempotency_key="bulk-cli-booking",
    )

    assert (
        main(
            [
                "--config",
                str(config),
                "--db",
                str(database_path),
                "bulk",
                "preview",
                "edit_fields",
                transaction["id"],
                "--patch-json",
                json.dumps({"note": {"mode": "replace", "text": "CLI reviewed"}}),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["changed_transaction_count"] == 1

    assert (
        main(
            [
                "--config",
                str(config),
                "--db",
                str(database_path),
                "bulk",
                "run",
                preview["preview_id"],
                "--fingerprint",
                preview["version_fingerprint"],
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert repository.get_transaction(transaction["id"])["note"] == "CLI reviewed"


def test_manual_transaction_rejects_provider_accounts(tmp_path):
    repository, service = _service(tmp_path)
    repository.import_transactions(
        "file",
        "Test Bank",
        [
            NormalizedTransaction(
                "booking-1",
                date(2026, 7, 5),
                Decimal("-19.99"),
                "EUR",
                "Kartenzahlung",
                "account-1",
            )
        ],
    )
    bank_account = repository.list_accounts()[0]
    with pytest.raises(ValueError, match="only allowed on manual accounts"):
        service.create_manual_transaction(
            bank_account["id"],
            booking_date=date(2026, 7, 6),
            amount=Decimal("-5.00"),
            purpose="Erfundene Buchung",
            idempotency_key="phantom-booking",
        )


def test_mcp_manual_transaction_requires_explicit_opt_in(tmp_path):
    config = _minimal_config(tmp_path)
    database_path = tmp_path / "workflow.db"
    repository = LedgerRepository(LedgerDatabase(database_path))
    install_builtin_profiles(repository)
    account = OrganizationService(repository).create_manual_account(
        "Bargeld",
        account_kind="cash",
        idempotency_key="mcp-manual-account",
    )
    service = MCPService(config_path=str(config), db_path=str(database_path))
    with pytest.raises(ValueError, match="disabled"):
        service.execute(
            "create_manual_transaction",
            {
                "account_id": account["id"],
                "booking_date": "2026-07-07",
                "amount": "-3.50",
                "purpose": "Bäckerei",
                "idempotency_key": "mcp-manual-booking",
            },
        )


def test_cli_and_mcp_expose_personal_organization_workflow(tmp_path, capsys):
    config = _minimal_config(tmp_path)
    database_path = tmp_path / "workflow.db"
    assert (
        main(
            [
                "--config",
                str(config),
                "--db",
                str(database_path),
                "accounts",
                "create-manual",
                "Bargeld",
                "--type",
                "cash",
                "--idempotency-key",
                "cli-account-001",
            ]
        )
        == 0
    )
    account_id = json.loads(capsys.readouterr().out)["id"]
    assert (
        main(
            [
                "--config",
                str(config),
                "--db",
                str(database_path),
                "manual",
                "add",
                account_id,
                "-3.50",
                "2026-07-07",
                "Bäckerei",
                "--idempotency-key",
                "cli-booking-001",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config),
                "--db",
                str(database_path),
                "search",
                "--text",
                "Bäckerei",
            ]
        )
        == 0
    )
    assert "Bäckerei" in capsys.readouterr().out

    service = MCPService(config_path=str(config), db_path=str(database_path))
    names = {definition[0] for definition in TOOL_DEFINITIONS}
    assert {
        "search_transactions",
        "preview_bulk_change",
        "list_review_items",
        "create_manual_account",
    }.issubset(names)
    bulk_schema = next(
        definition[2] for definition in TOOL_DEFINITIONS if definition[0] == "preview_bulk_change"
    )
    assert "edit_fields" in bulk_schema["properties"]["action"]["enum"]
    run_schema = next(
        definition[2] for definition in TOOL_DEFINITIONS if definition[0] == "run_bulk_change"
    )
    assert "fingerprint" in run_schema["properties"]
    response = service.execute("search_transactions", {"text": "Bäckerei"})
    assert len(response["transactions"]) == 1


def test_search_and_count_agree_on_every_filter(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="search-count-parity"
    )
    service.create_tag("Beleg")
    service.create_dimension_value("person", "person-b", "Person B")
    for index in range(4):
        transaction = service.create_manual_transaction(
            account["id"],
            booking_date=date(2026, 8, index + 1),
            amount=Decimal(f"-{index + 1}0"),
            purpose=f"Einkauf {index}",
            idempotency_key=f"search-count-transaction-{index}",
        )
        if index % 2 == 0:
            service.tag_transaction(
                transaction["id"], "Beleg", expected_version=transaction["version"]
            )
    service.set_transaction_note(
        service.search_transactions(TransactionSearchFilter())[0]["id"],
        "Sondernotiz",
        expected_version=1,
    )

    filters = [
        TransactionSearchFilter(),
        TransactionSearchFilter(text="Einkauf"),
        TransactionSearchFilter(text="Sondernotiz"),
        TransactionSearchFilter(booking_from="2026-08-02"),
        TransactionSearchFilter(amount_min_minor=-2500),
        TransactionSearchFilter(tags=("Beleg",)),
        TransactionSearchFilter(currencies=("EUR",)),
        TransactionSearchFilter(document_status="unmatched"),
    ]
    for item in filters:
        assert len(service.search_transactions(item, limit=500)) == service.count_transactions(item)

from __future__ import annotations

import re
import socket
import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dagoberd.config import load_config
from dagoberd.gui import create_app
from dagoberd.gui.security import ensure_port_available
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.organization import OrganizationService
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.ledger.semantics import TransactionSemanticsService
from dagoberd.llm.openai_compatible import LLMProviderError
from dagoberd.profiles import install_builtin_profiles
from dagoberd.providers.comdirect import ComdirectAPIError
from dagoberd.services.comdirect_sync import SyncResult

COMDIRECT_CONFIG = """
[comdirect.connections.private]
enabled = true
username_secret = "comdirect.private.username"
client_id_secret = "comdirect.private.client-id"
client_secret_secret = "comdirect.private.client-secret"
"""


def _wait_for(client: TestClient, headers: dict[str, str], job_id: str, status: str) -> dict:
    for _ in range(200):
        payload = client.get(f"/api/jobs/{job_id}", headers=headers).json()["data"]
        if payload["status"] == status:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"Job never reached {status}")


def _client(
    tmp_path: object, extra_config: str = ""
) -> tuple[TestClient, dict[str, str], OrganizationService, str]:
    config = tmp_path / "account-eval.toml"
    config.write_text(
        """[database]
path = "ledger.db"
[documents]
storage_dir = "documents"
"""
        + extra_config,
        encoding="utf-8",
    )
    loaded = load_config(config)
    repository = LedgerRepository(LedgerDatabase(loaded.config.database.path))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "GUI", account_kind="cash", idempotency_key="gui-account-001"
    )
    client = TestClient(create_app(loaded, "test-token"))
    return client, {"X-AccountEval-Token": "test-token"}, service, account["id"]


def test_gui_exposes_safe_model_failures(tmp_path, monkeypatch):
    client, headers, _, _ = _client(tmp_path)

    class FailingProvider:
        def test_connection(self):
            raise LLMProviderError("LLM connection test failed")

    monkeypatch.setattr(
        "dagoberd.gui.context.GuiContext.llm_provider",
        lambda _: FailingProvider(),
    )
    response = client.post("/api/llm/test", headers=headers)
    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "llm_failed",
        "message": "LLM connection test failed",
        "details": None,
    }


def test_gui_requires_token_and_exposes_metadata(tmp_path):
    client, headers, _service, account_id = _client(tmp_path)

    assert client.get("/api/meta").status_code == 401
    response = client.get("/api/meta", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["contract_version"] == "account-eval-gui-v5"
    assert any(account["id"] == account_id for account in response.json()["data"]["accounts"])
    assert response.json()["data"]["account_groups"] == []


def test_gui_composite_bulk_context_preview_and_fingerprint_run(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    first = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-10"),
        purpose="First",
        idempotency_key="gui-bulk-edit-first",
    )
    second = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 2),
        amount=Decimal("-20"),
        purpose="Second",
        idempotency_key="gui-bulk-edit-second",
    )
    transaction_ids = [first["id"], second["id"]]

    context = client.post(
        "/api/bulk/context", headers=headers, json={"transaction_ids": transaction_ids}
    )
    assert context.status_code == 200, context.text
    assert context.json()["data"]["transaction_count"] == 2

    preview = client.post(
        "/api/bulk/preview",
        headers=headers,
        json={
            "action": "edit_fields",
            "transaction_ids": transaction_ids,
            "payload": {"note": {"mode": "replace", "text": "Together"}},
        },
    )
    assert preview.status_code == 200, preview.text
    prepared = preview.json()["data"]
    assert prepared["changed_transaction_count"] == 2
    missing_fingerprint = client.post(f"/api/bulk/{prepared['preview_id']}/run", headers=headers)
    assert missing_fingerprint.status_code == 400

    executed = client.post(
        f"/api/bulk/{prepared['preview_id']}/run",
        headers=headers,
        json={"fingerprint": prepared["version_fingerprint"]},
    )
    assert executed.status_code == 200, executed.text
    rows = executed.json()["data"]["updated_rows"]
    assert {row["note"] for row in rows} == {"Together"}

    stale = client.post(
        "/api/bulk/preview",
        headers=headers,
        json={
            "action": "edit_fields",
            "transaction_ids": transaction_ids,
            "payload": {"note": {"mode": "clear"}},
        },
    ).json()["data"]
    first_row = next(row for row in rows if row["id"] == first["id"])
    service.set_transaction_note(
        first["id"], "Changed elsewhere", expected_version=first_row["version"]
    )
    conflict = client.post(
        f"/api/bulk/{stale['preview_id']}/run",
        headers=headers,
        json={"fingerprint": stale["version_fingerprint"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["transaction_ids"] == [first["id"]]


def test_gui_static_assets_are_never_served_from_a_stale_cache(tmp_path):
    client, _headers, _service, _account_id = _client(tmp_path)

    index = client.get("/")
    asset_path = re.search(r"(/assets/[0-9a-f]{16}/app\.js)", index.text)

    assert index.status_code == 200
    assert asset_path is not None
    application = client.get(asset_path.group(1))
    assert application.status_code == 200
    assert index.headers["cache-control"] == "no-store, max-age=0"
    assert index.headers["clear-site-data"] == '"cache"'
    assert application.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "__ACCOUNT_EVAL_ASSETS__" not in index.text
    assert "The interface could not be started" in index.text


def test_gui_rejects_a_port_used_by_an_older_process():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        with pytest.raises(ValueError, match="previous GUI"):
            ensure_port_available("127.0.0.1", listener.getsockname()[1])


def test_gui_port_can_be_reused_immediately_after_listener_closes():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()

    ensure_port_available("127.0.0.1", port)


def test_dashboard_summarizes_scoped_balances_and_cashflow(tmp_path):
    client, headers, service, asset_id = _client(tmp_path)
    liability = service.create_manual_account(
        "Card", account_kind="credit_card", idempotency_key="gui-account-card-001"
    )
    service.repository.record_balance_snapshot(
        asset_id, 100_000, balance_date="2026-08-31", external_id="asset-balance"
    )
    service.repository.record_balance_snapshot(
        liability["id"], -20_000, balance_date="2026-08-31", external_id="card-balance"
    )
    service.create_manual_transaction(
        asset_id,
        booking_date=date(2026, 8, 2),
        amount=Decimal("300"),
        purpose="Income",
        idempotency_key="gui-dashboard-income",
    )
    service.create_manual_transaction(
        asset_id,
        booking_date=date(2026, 8, 3),
        amount=Decimal("-100"),
        purpose="Expense",
        idempotency_key="gui-dashboard-expense",
    )
    outside = service.create_manual_account(
        "Outside scope", account_kind="cash", idempotency_key="gui-account-outside-scope"
    )
    service.create_manual_transaction(
        outside["id"],
        booking_date=date(2026, 8, 4),
        amount=Decimal("1"),
        purpose="Outside review",
        idempotency_key="gui-dashboard-outside-review",
    )

    response = client.get(
        "/api/dashboard",
        headers=headers,
        params=[
            ("account_id", asset_id),
            ("account_id", liability["id"]),
            ("period", "12m"),
            ("as_of", "2026-08-31"),
        ],
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {
        "currency": "EUR",
        "complete": True,
        "assets_minor": 100_000,
        "liabilities_minor": 20_000,
        "net_worth_minor": 80_000,
        "current_month": {
            "income_minor": 30_000,
            "expense_minor": -10_000,
            "net_minor": 20_000,
            "complete": True,
        },
    }
    assert {item["section"] for item in data["accounts"]} == {"assets", "liabilities"}
    assert data["net_worth_series"][-1]["net_worth_minor"] == 80_000
    assert data["cashflow_series"][-1]["net_minor"] == 20_000
    assert data["quality"]["open_review_count"] == 2


def test_dashboard_with_missing_balance_is_explicitly_incomplete(tmp_path):
    client, headers, _service, account_id = _client(tmp_path)

    response = client.get(
        "/api/dashboard",
        headers=headers,
        params={"account_id": account_id, "as_of": "2026-08-31"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"]["complete"] is False
    assert data["summary"]["net_worth_minor"] is None
    assert data["quality"]["missing_balance_count"] == 1
    assert data["accounts"][0]["section"] == "missing"


def test_dashboard_reports_stale_balance_and_missing_exchange_rate(tmp_path):
    client, headers, service, euro_id = _client(
        tmp_path,
        """[reporting]
base_currency = "EUR"
""",
    )
    usd = service.create_manual_account(
        "USD wallet",
        account_kind="cash",
        currency="USD",
        idempotency_key="gui-account-usd-001",
    )
    service.repository.record_balance_snapshot(
        euro_id, 10_000, balance_date="2026-01-01", external_id="stale-eur-balance"
    )
    service.repository.record_balance_snapshot(
        usd["id"], 20_000, balance_date="2026-08-31", external_id="usd-balance"
    )

    response = client.get(
        "/api/dashboard",
        headers=headers,
        params=[
            ("account_id", euro_id),
            ("account_id", usd["id"]),
            ("as_of", "2026-08-31"),
        ],
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"]["complete"] is False
    assert data["summary"]["net_worth_minor"] is None
    assert data["quality"]["stale_balance_count"] == 1
    assert data["quality"]["missing_conversion_count"] == 1


def test_dashboard_requires_report_currency_for_multi_currency_scope(tmp_path):
    client, headers, service, euro_id = _client(tmp_path)
    usd = service.create_manual_account(
        "USD wallet",
        account_kind="cash",
        currency="USD",
        idempotency_key="gui-account-usd-no-base",
    )
    for account_id, balance, external_id in (
        (euro_id, 10_000, "euro-no-base"),
        (usd["id"], 20_000, "usd-no-base"),
    ):
        service.repository.record_balance_snapshot(
            account_id,
            balance,
            balance_date="2026-08-31",
            external_id=external_id,
        )

    data = client.get(
        "/api/dashboard",
        headers=headers,
        params=[
            ("account_id", euro_id),
            ("account_id", usd["id"]),
            ("as_of", "2026-08-31"),
        ],
    ).json()["data"]

    assert data["summary"]["complete"] is False
    assert data["summary"]["currency"] is None
    assert data["quality"]["missing_display_currency"] is True


def test_dashboard_cashflow_excludes_confirmed_transfers(tmp_path):
    client, headers, service, source_id = _client(tmp_path)
    target = service.create_manual_account(
        "Savings",
        account_kind="savings",
        idempotency_key="gui-account-transfer-target",
    )
    for account_id, amount, key in (
        (source_id, "-100", "gui-dashboard-transfer-out"),
        (target["id"], "100", "gui-dashboard-transfer-in"),
    ):
        service.create_manual_transaction(
            account_id,
            booking_date=date(2026, 8, 5),
            amount=Decimal(amount),
            purpose="Internal transfer",
            idempotency_key=key,
        )
    relation = TransactionSemanticsService(service.repository).suggest("transfer")[0]
    service.repository.review_transaction_relation(relation["id"], "confirm", relation["version"])

    data = client.get(
        "/api/dashboard",
        headers=headers,
        params={"period": "3m", "as_of": "2026-08-31"},
    ).json()["data"]

    august = next(item for item in data["cashflow_series"] if item["month"] == "2026-08")
    assert august["income_minor"] == 0
    assert august["expense_minor"] == 0
    assert august["net_minor"] == 0


def test_dashboard_periods_and_meta_account_groups(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    group_id = service.repository.create_account_group("Liquid", [account_id], "Daily")
    service.repository.record_balance_snapshot(
        account_id, 1_000, balance_date="2026-08-31", external_id="period-balance"
    )

    ytd = client.get(
        "/api/dashboard",
        headers=headers,
        params={"account_id": account_id, "period": "ytd", "as_of": "2026-08-31"},
    )
    three_months = client.get(
        "/api/dashboard",
        headers=headers,
        params={"account_id": account_id, "period": "3m", "as_of": "2026-08-31"},
    )
    metadata = client.get("/api/meta", headers=headers).json()["data"]

    assert ytd.json()["data"]["scope"]["booking_from"] == "2026-01-01"
    assert three_months.json()["data"]["scope"]["booking_from"] == "2026-06-01"
    assert len(metadata["account_groups"]) == 1
    group = metadata["account_groups"][0]
    assert group["id"] == group_id
    assert group["name"] == "Liquid"
    assert group["description"] == "Daily"
    assert group["account_ids"] == [account_id]


def test_gui_search_note_and_version_conflict(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    transaction = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-12.50"),
        purpose="Groceries",
        idempotency_key="gui-transaction-001",
    )

    initial = client.get(f"/api/transactions?account_id={account_id}", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["data"]["total"] == 1
    note = client.post(
        f"/api/transactions/{transaction['id']}/note",
        headers=headers,
        json={"note": "weekly", "expected_version": transaction["version"]},
    )
    assert note.status_code == 200
    assert client.get("/api/transactions?text=weekly", headers=headers).json()["data"]["total"] == 1

    conflict = client.post(
        f"/api/transactions/{transaction['id']}/note",
        headers=headers,
        json={"note": "stale", "expected_version": transaction["version"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"


def test_gui_search_applies_account_and_date_filters(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("1"),
        purpose="Old",
        idempotency_key="gui-filter-001",
    )
    service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 2),
        amount=Decimal("2"),
        purpose="New",
        idempotency_key="gui-filter-002",
    )

    response = client.get(
        f"/api/transactions?account_id={account_id}&booking_from=2026-08-02", headers=headers
    )

    assert response.json()["data"]["total"] == 1
    assert response.json()["data"]["items"][0]["purpose"] == "New"


def test_gui_search_supports_stable_sorting(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    for key, amount in (("first", "2"), ("second", "1"), ("third", "2")):
        service.create_manual_transaction(
            account_id,
            booking_date=date(2026, 8, 1),
            amount=Decimal(amount),
            purpose=key,
            idempotency_key=f"gui-sort-{key}",
        )

    first_page = client.get(
        "/api/transactions",
        headers=headers,
        params={
            "account_id": account_id,
            "sort_by": "amount",
            "sort_direction": "asc",
            "limit": 2,
        },
    )
    second_page = client.get(
        "/api/transactions",
        headers=headers,
        params={
            "account_id": account_id,
            "sort_by": "amount",
            "sort_direction": "asc",
            "limit": 2,
            "offset": 2,
        },
    )

    assert first_page.status_code == 200
    first_items = first_page.json()["data"]["items"]
    second_items = second_page.json()["data"]["items"]
    assert [item["amount_minor"] for item in first_items] == [100, 200]
    assert len({item["id"] for item in [*first_items, *second_items]}) == 3
    assert "has_open_review" in first_items[0]
    row = client.get(f"/api/transactions/{first_items[0]['id']}/row", headers=headers)
    assert row.json()["data"] == first_items[0]


def test_sync_job_completes_after_browser_tan(tmp_path, monkeypatch):
    client, headers, _service, _account_id = _client(tmp_path, COMDIRECT_CONFIG)

    def fake_synchronize(*_args, photo_tan, **_kwargs):
        photo_tan(b"challenge-image")
        return SyncResult(sync_run_id="sync-1", imported=2, skipped=0, accounts=1)

    monkeypatch.setattr("dagoberd.gui.routes.operations.synchronize", fake_synchronize)
    job_id = client.post(
        "/api/sync/comdirect",
        headers=headers,
        json={"connection": "private", "pin": "0000"},
    ).json()["data"]["job_id"]

    awaiting = _wait_for(client, headers, job_id, "awaiting_tan")
    assert awaiting["challenge"]["kind"] == "P_TAN"
    assert awaiting["challenge"]["image_base64"]
    client.post(f"/api/jobs/{job_id}/tan", headers=headers, json={"tan": "123456"})

    finished = _wait_for(client, headers, job_id, "succeeded")
    assert finished["result"]["imported"] == 2


def test_failed_sync_job_releases_the_registry_slot(tmp_path, monkeypatch):
    client, headers, _service, _account_id = _client(tmp_path, COMDIRECT_CONFIG)

    def failing_synchronize(*_args, **_kwargs):
        raise ComdirectAPIError("Comdirect meldet 500")

    monkeypatch.setattr("dagoberd.gui.routes.operations.synchronize", failing_synchronize)
    request = {"connection": "private", "pin": "0000"}
    job_id = client.post("/api/sync/comdirect", headers=headers, json=request).json()["data"][
        "job_id"
    ]

    failed = _wait_for(client, headers, job_id, "failed")
    assert failed["error"] == "Comdirect meldet 500"
    assert client.post("/api/sync/comdirect", headers=headers, json=request).status_code == 200


def test_automatic_classification_applies_profile_rules(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    transaction = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-30"),
        purpose="Supermarkt Einkauf",
        idempotency_key="gui-classify-001",
    )
    service.repository.add_rule("household", "Supermarkt", "Supermarkt", "expense.food")

    response = client.post(
        "/api/classification/auto",
        headers=headers,
        json={"profile_code": "household"},
    )

    assert response.status_code == 200
    detail = client.get(f"/api/transactions/{transaction['id']}", headers=headers).json()["data"]
    assert detail["classifications"][0]["category_code"] == "expense.food"


def test_classification_configuration_scopes_rules_and_explains_pipeline(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    transaction = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-30"),
        purpose="Scoped grocery",
        idempotency_key="gui-scoped-rule-001",
    )
    created = client.post(
        "/api/rules",
        headers=headers,
        json={
            "profile_code": "household",
            "name": "Scoped",
            "pattern": "grocery",
            "category_code": "expense.food",
            "account_id": account_id,
        },
    )
    assert created.status_code == 200
    rule = next(
        item
        for item in client.get(f"/api/rules?account_id={account_id}", headers=headers).json()[
            "data"
        ]
        if item["name"] == "Scoped"
    )
    assert rule["account_id"] == account_id
    assert (
        client.post(
            "/api/classification/explain",
            headers=headers,
            json={"transaction_id": transaction["id"], "profile_code": "household"},
        ).json()["data"][0]["status"]
        == "match"
    )
    disabled = client.put(
        f"/api/accounts/{account_id}/stages/rule",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    explanation = client.post(
        "/api/classification/explain",
        headers=headers,
        json={"transaction_id": transaction["id"], "profile_code": "household"},
    ).json()["data"]
    assert explanation[0] == {"stage": "rule", "status": "disabled"}
    updated = client.patch(
        f"/api/rules/{rule['id']}",
        headers=headers,
        json={"expected_version": rule["version"], "active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["account_id"] == account_id


def test_classification_configuration_api_validates_and_deletes_entries(tmp_path):
    client, headers, _service, account_id = _client(tmp_path)
    created = client.post(
        "/api/rules",
        headers=headers,
        json={
            "profile_code": "household",
            "name": "Delete me",
            "pattern": "delete",
            "category_code": "expense.food",
        },
    )
    assert created.status_code == 200
    rule = next(
        item
        for item in client.get("/api/rules", headers=headers).json()["data"]
        if item["name"] == "Delete me"
    )
    assert (
        client.delete(
            f"/api/rules/{rule['id']}?expected_version={rule['version']}", headers=headers
        ).status_code
        == 200
    )
    hint = client.post(
        "/api/hints",
        headers=headers,
        json={"profile_code": "household", "text": "LLM-only context", "account_id": account_id},
    )
    assert hint.status_code == 200
    invalid = client.post(
        "/api/classification/rules/test",
        headers=headers,
        json={"pattern": "[", "profile_code": "household"},
    )
    assert invalid.status_code == 400
    llm = client.get("/api/llm", headers=headers).json()["data"]
    assert "api_key" not in llm
    assert llm["api_key_secret"] == "ACCOUNT_EVAL_LLM_API_KEY"


def test_dimension_columns_follow_account_order_and_active_values(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    service.create_dimension("second", "Second")
    service.create_dimension_value("person", "person-b", "Person B")
    service.create_dimension_value("second", "x", "X")
    configured = client.put(
        f"/api/accounts/{account_id}/columns",
        headers=headers,
        json={"dimension_codes": ["second", "person"]},
    )
    assert [item["code"] for item in configured.json()["data"]] == ["second", "person"]
    value = next(
        item
        for item in client.get("/api/meta", headers=headers).json()["data"]["dimensions"]
        if item["code"] == "person"
    )["values"][0]
    assert (
        client.patch(
            "/api/dimensions/person/values/person-b",
            headers=headers,
            json={"active": False, "expected_version": value["version"]},
        ).status_code
        == 200
    )
    options = client.get(f"/api/accounts/{account_id}/options", headers=headers).json()["data"]
    assert [item["code"] for item in options["dimensions"]] == ["second"]
    assert "person" not in options["dimension_value"]


def test_every_attribute_edit_advances_the_transaction_version(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    service.create_dimension_value("person", "person-b", "Person B")
    service.create_tag("Document")
    service.create_merchant("Rewe")
    transaction = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-30"),
        purpose="Einkauf",
        idempotency_key="gui-edit-001",
    )
    edits = [
        ("dimensions", {"dimension_code": "person", "value_code": "person-b"}),
        ("category", {"profile_code": "household", "category_code": "expense.food"}),
        ("tags", {"tag": "Document"}),
        ("merchant", {"merchant": "Rewe"}),
        ("note", {"note": "Wocheneinkauf"}),
        ("status", {"status": "corrected"}),
    ]
    version = transaction["version"]
    for path, body in edits:
        response = client.post(
            f"/api/transactions/{transaction['id']}/{path}",
            headers=headers,
            json={**body, "expected_version": version},
        )
        assert response.status_code == 200, (path, response.json())
        version = response.json()["data"]["version"]

    item = client.get(f"/api/transactions?account_id={account_id}", headers=headers).json()["data"][
        "items"
    ][0]
    assert item["dimensions"] == {"person": "person-b"}
    assert (item["category_code"], item["classification_profile"]) == ("expense.food", "household")
    assert item["tags"] == ["Document"]
    assert (item["merchant"], item["note"], item["booking_status"]) == (
        "Rewe",
        "Wocheneinkauf",
        "corrected",
    )
    assert item["version"] == version


def test_dimension_whitelist_narrows_only_its_own_dimension(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    service.create_dimension_value("person", "person-b", "Person B")
    service.create_dimension_value("person", "sam", "Sam")
    service.create_dimension_value("project", "haus", "Haus")

    client.put(
        f"/api/accounts/{account_id}/options",
        headers=headers,
        json={"field_kind": "dimension_value", "codes": ["person:person-b"]},
    )
    narrowed = client.get(f"/api/accounts/{account_id}/options", headers=headers).json()["data"]
    assert narrowed["dimension_value"]["person"] == [{"code": "person-b", "name": "Person B"}]
    assert narrowed["dimension_value"]["project"] == [{"code": "haus", "name": "Haus"}]
    assert client.get(f"/api/accounts/{account_id}/options/selected", headers=headers).json()[
        "data"
    ]["dimension_value"] == ["person:person-b"]

    client.put(
        f"/api/accounts/{account_id}/options",
        headers=headers,
        json={"field_kind": "dimension_value", "codes": []},
    )
    restored = client.get(f"/api/accounts/{account_id}/options", headers=headers).json()["data"]
    assert len(restored["dimension_value"]["person"]) == 2


def test_import_round_trip_adds_transactions(tmp_path):
    client, headers, _service, account_id = _client(tmp_path)
    source = tmp_path / "buchungen.csv"
    source.write_text(
        "booking_date;amount;currency;purpose;reference\n"
        "2026-08-10;-19,99;EUR;Drogerie;R-1001\n"
        "2026-08-11;-5,50;EUR;Baeckerei;R-1002\n",
        encoding="utf-8",
    )

    with source.open("rb") as handle:
        upload = client.post(
            "/api/imports/upload", headers=headers, files={"file": ("buchungen.csv", handle)}
        ).json()["data"]
    preview = client.post(
        "/api/imports/preview",
        headers=headers,
        json={"source": "tabular", "account_id": account_id, "path": upload["path"]},
    ).json()["data"]
    assert (preview["count"], preview["error_count"]) == (2, 0)
    executed = client.post(f"/api/imports/{preview['preview_id']}/run", headers=headers).json()[
        "data"
    ]

    assert executed["imported"] == 2
    listing = client.get(f"/api/transactions?account_id={account_id}", headers=headers)
    assert listing.json()["data"]["total"] == 2


def test_assigned_document_marks_the_transaction(tmp_path):
    client, headers, service, account_id = _client(tmp_path)
    transaction = service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-30"),
        purpose="Einkauf",
        idempotency_key="gui-document-001",
    )
    invoice = tmp_path / "rechnung.pdf"
    invoice.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )

    with invoice.open("rb") as handle:
        document = client.post(
            "/api/documents/upload", headers=headers, files={"file": ("rechnung.pdf", handle)}
        ).json()["data"]
    assigned = client.post(
        f"/api/documents/{document['id']}/assign",
        headers=headers,
        json={"transaction_id": transaction["id"]},
    )

    assert assigned.status_code == 200
    assert assigned.json()["data"]["status"] == "matched"
    detail = client.get(f"/api/transactions/{transaction['id']}", headers=headers).json()["data"]
    assert [item["match_status"] for item in detail["documents"]] == ["matched"]
    assert client.get(f"/api/documents/{document['id']}/file", headers=headers).status_code == 200


def test_document_batch_preview_and_run_use_the_same_fingerprint(tmp_path):
    client, headers, _service, _account_id = _client(tmp_path)
    source = tmp_path / "document-source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"\xff\xd8\xffgui-synthetic")

    preview_response = client.post(
        "/api/documents/batch/preview",
        headers=headers,
        params={"path": str(source)},
    )
    preview = preview_response.json()["data"]
    executed = client.post(
        f"/api/documents/batch/{preview['preview_id']}/run",
        headers=headers,
        json={"fingerprint": preview["fingerprint"]},
    )

    assert preview_response.status_code == 200
    assert executed.status_code == 200
    assert executed.json()["data"]["created_documents"] == 1

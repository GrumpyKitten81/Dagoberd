from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from account_eval.accounts import AccountScope
from account_eval.cli import main
from account_eval.config import PrivacyConfig
from account_eval.documents.matching import DocumentMatcher
from account_eval.ledger.classification import ClassificationService
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.llm.base import (
    LLMClassificationInput,
    LLMClassificationSuggestion,
)
from account_eval.mcp_server import TOOL_DEFINITIONS, MCPService
from account_eval.profiles import install_builtin_profiles
from account_eval.reporting.ledger_reports import LedgerReportGenerator


class FakeLLM:
    def __init__(self) -> None:
        self.items: list[LLMClassificationInput] = []

    def classify(self, item: LLMClassificationInput):
        self.items.append(item)
        return LLMClassificationSuggestion(
            category="expense.business",
            confidence=0.8,
            rationale="Testvorschlag",
            provider="fake",
            model="fake-local",
            prompt_version="test-v1",
            prompt_hash="abc123",
            taxonomy_version=item.taxonomy_version,
            duration_ms=0,
        )

    def test_connection(self):
        return {"status": "ok"}


def _repo(tmp_path) -> LedgerRepository:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    return repository


def _transaction(
    external_id: str,
    account: str,
    amount: str,
    purpose: str,
    currency: str = "EUR",
) -> NormalizedTransaction:
    return NormalizedTransaction(
        external_id,
        date(2026, 7, 1),
        Decimal(amount),
        currency,
        purpose,
        account,
        counterparty="ACME",
        reference=external_id,
    )


def test_llm_is_redacted_and_remains_reviewable_suggestion(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file",
        "test",
        [_transaction("invoice-123", "one", "-20.00", "IBAN DE02120300000000202051 123456789")],
    )
    transaction = repository.list_transactions()[0]
    fake = FakeLLM()

    identifier = ClassificationService(
        repository,
        fake,
        PrivacyConfig(max_text_length=200),
    ).classify(transaction["id"], "freelance")

    assert identifier is not None
    assert "DE02120300000000202051" not in fake.items[0].purpose
    suggestion = repository.list_classifications(status="suggested")[0]
    assert suggestion["model_version"] == "fake/fake-local"
    assert suggestion["prompt_hash"] == "abc123"
    reviewed = repository.review_classification(
        identifier,
        "accept",
        suggestion["transaction_version"],
    )
    assert reviewed["status"] == "applied"
    with pytest.raises(ValueError):
        repository.review_classification(
            identifier,
            "accept",
            suggestion["transaction_version"],
        )


def test_hint_priority_and_split_revert(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file", "test", [_transaction("one", "one", "-100.00", "Speziallieferant")]
    )
    transaction = repository.list_transactions()[0]
    repository.add_hint(
        "freelance",
        "Speziallieferant ist betrieblich",
        pattern="Speziallieferant",
        category_code="expense.business",
    )
    result = ClassificationService(repository).classify(transaction["id"], "freelance")
    assert result is not None
    applied = repository.list_classifications(status="applied")[0]
    assert applied["source"] == "hint"

    current = repository.list_transactions()[0]
    repository.split_transaction(
        current["id"],
        "freelance",
        [
            (Decimal("-70.00"), "expense.business", "A"),
            (Decimal("-30.00"), "expense.business", "B"),
        ],
        current["version"],
    )
    version = repository.get_transaction(current["id"])["version"]
    repository.revert_split(current["id"], version)
    assert repository.get_transaction(current["id"])["state"] == "active"


def test_document_hash_review_and_unique_auto_match(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file", "test", [_transaction("INV-1", "one", "-42.00", "ACME Rechnung")]
    )
    document = tmp_path / "invoice.txt"
    document.write_text("local document", encoding="utf-8")
    matcher = DocumentMatcher(repository, tmp_path / "documents")
    first = matcher.import_file(
        document,
        amount=Decimal("42.00"),
        document_date=date(2026, 7, 1),
        merchant="ACME",
        reference="INV-1",
    )
    second = matcher.import_file(document)
    assert first["created"] is True
    assert second["created"] is False
    candidates = matcher.suggest(first["id"])
    assert candidates[0]["status"] == "matched"
    unmatched = matcher.unmatch(candidates[0]["match_id"], candidates[0]["version"])
    assert unmatched["status"] == "suggested"


def test_reports_keep_account_scope_and_currency_separate(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file",
        "test",
        [
            _transaction("one", "account-one", "100.00", "Income"),
            _transaction("two", "account-two", "-25.00", "Expense"),
        ],
    )
    accounts = repository.list_accounts()
    selected = AccountScope.selected([accounts[0]["id"]])
    paths = LedgerReportGenerator(repository, tmp_path / "reports").generate(
        formats=("json",), account_scope=selected
    )
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["metadata"]["account_scope"]["account_ids"] == [accounts[0]["id"]]
    assert len(payload["transactions"]) == 1
    assert payload["categories"][0]["Währung"] == "EUR"


def test_report_defaults_to_enabled_accounts(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file",
        "test",
        [
            _transaction("active", "account-active", "100.00", "Income"),
            _transaction("disabled", "account-disabled", "-25.00", "Expense"),
        ],
    )
    accounts = {account["external_id"]: account for account in repository.list_accounts()}
    disabled = accounts["account-disabled"]
    repository.set_account_enabled(
        disabled["id"],
        False,
        expected_version=disabled["version"],
    )

    path = LedgerReportGenerator(repository, tmp_path / "reports").generate(formats=("json",))[0]
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert [row["purpose"] for row in payload["transactions"]] == ["Income"]
    assert payload["metadata"]["account_scope"] == {
        "mode": "all",
        "account_ids": [],
    }


def test_cli_report_applies_currency_filter(tmp_path):
    repository = _repo(tmp_path)
    repository.import_transactions(
        "file",
        "test",
        [
            _transaction("eur", "account-eur", "100.00", "EUR income"),
            _transaction(
                "usd",
                "account-usd",
                "25.00",
                "USD income",
                currency="USD",
            ),
        ],
    )
    output_dir = tmp_path / "reports"

    assert (
        main(
            [
                "--db",
                str(repository.database.path),
                "report",
                "--output",
                str(output_dir),
                "--formats",
                "json",
                "--currency",
                "EUR",
            ]
        )
        == 0
    )

    payload = json.loads((output_dir / "general-report.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["currency_filter"] == "EUR"
    assert [row["currency"] for row in payload["transactions"]] == ["EUR"]


def test_mcp_contract_covers_full_journey_without_secret_arguments(tmp_path):
    service = MCPService(db_path=str(tmp_path / "mcp.db"))
    status = service.execute("get_status", {})
    assert "configuration" in status
    names = {name for name, _description, _schema in TOOL_DEFINITIONS}
    assert {
        "preview_account_import",
        "sync_comdirect",
        "import_document",
        "preview_document_batch",
        "run_document_batch",
        "preview_document_matches",
        "apply_document_match_preview",
        "run_classification",
        "create_split",
        "generate_report",
    }.issubset(names)
    serialized = json.dumps(TOOL_DEFINITIONS).casefold()
    assert 'tan"' not in serialized
    assert "password" not in serialized
    first = service.execute(
        "add_rule",
        {
            "profile": "freelance",
            "name": "Idempotent",
            "pattern": "TEST-IDEMPOTENT",
            "category": "expense.business",
            "idempotency_key": "test-idempotency-1",
        },
    )
    second = service.execute(
        "add_rule",
        {
            "profile": "freelance",
            "name": "Idempotent",
            "pattern": "TEST-IDEMPOTENT",
            "category": "expense.business",
            "idempotency_key": "test-idempotency-1",
        },
    )
    assert first == second

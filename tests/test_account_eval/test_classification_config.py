from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from account_eval.cli import main
from account_eval.ledger.classification import ClassificationService
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.pipeline import PipelineConfig
from account_eval.ledger.repository import LedgerRepository, VersionConflictError
from account_eval.llm.openai_compatible import LLMProviderError
from account_eval.profiles import install_builtin_profiles


class CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, _item):
        self.calls += 1
        return None

    def test_connection(self):
        return {"status": "ok"}


def _setup(tmp_path) -> tuple[LedgerRepository, OrganizationService, str]:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repository)
    service = OrganizationService(repository)
    account = service.create_manual_account(
        "Cash", account_kind="cash", idempotency_key="classification-config-account"
    )
    return repository, service, account["id"]


def _transaction(service: OrganizationService, account_id: str, key: str, purpose: str) -> dict:
    return service.create_manual_transaction(
        account_id,
        booking_date=date(2026, 8, 1),
        amount=Decimal("-10"),
        purpose=purpose,
        idempotency_key=key,
    )


def test_cli_reports_model_failures_without_storing_suggestions(tmp_path, monkeypatch, capsys):
    repository, service, account_id = _setup(tmp_path)
    transaction = _transaction(service, account_id, "invalid-model-output", "Unknown vendor")
    config = tmp_path / "account-eval.toml"
    config.write_text('[database]\npath="ledger.db"\n', encoding="utf-8")

    class FailingProvider:
        def classify(self, _item):
            raise LLMProviderError("LLM returned invalid structured output")

    monkeypatch.setattr("account_eval.cli.create_llm_provider", lambda *_: FailingProvider())
    assert (
        main(
            [
                "--config",
                str(config),
                "--output",
                "json",
                "classifications",
                "run",
                "household",
                "--transaction",
                transaction["id"],
            ]
        )
        == 6
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "llm_failed"
    assert error["message"] == "LLM returned invalid structured output"
    assert repository.list_classifications(status="suggested") == []


def test_explain_matches_classification_and_inactive_profile_is_read_only(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    transaction = _transaction(service, account_id, "explain-transaction", "Review grocery")
    repository.add_rule("household", "Grocery", "grocery", "expense.food")
    classification = ClassificationService(repository)

    before = len(repository.list_classifications())
    explanation = classification.explain(transaction["id"], "household")
    assert explanation[0]["status"] == "match"
    assert explanation[0]["category_code"] == "expense.food"
    assert len(repository.list_classifications()) == before

    classification.classify_deterministic(transaction["id"], "household")
    applied = repository.list_classifications(status="applied")
    assert applied[0]["source"] == explanation[0]["stage"]
    assert applied[0]["category_code"] == explanation[0]["category_code"]

    PipelineConfig(repository).set_account_profiles(account_id, ["rental"])
    inactive = classification.explain(transaction["id"], "household")
    assert {entry["status"] for entry in inactive} == {"profile_inactive"}
    assert classification.classify_deterministic(transaction["id"], "household") is None


def test_rule_crud_validation_and_deterministic_ties(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    transaction = _transaction(service, account_id, "tie-transaction", "Tie merchant")
    with pytest.raises(ValueError, match="Invalid regular expression"):
        repository.add_rule("household", "Bad", "[", "expense.food")
    with pytest.raises(ValueError, match="does not belong"):
        repository.add_rule("household", "Wrong profile", "tie", "income.rent")
    with pytest.raises(ValueError, match="Invalid regular expression"):
        repository.add_hint("household", "Bad hint", pattern="[", category_code="expense.food")

    first = repository.add_rule("household", "First", "tie", "expense.food", priority=7)
    second = repository.add_rule("household", "Second", "tie", "expense.children", priority=7)
    classification = ClassificationService(repository)
    matches = [
        classification.find_rule(repository.get_transaction(transaction["id"]), "household")
        for _ in range(5)
    ]
    assert {match.category_code for match in matches} == {matches[0].category_code}
    winner_id = matches[0].source_id
    loser_id = second if winner_id == first else first
    rule = next(item for item in repository.list_rules() if item["id"] == winner_id)
    with pytest.raises(ValueError, match="Invalid regular expression"):
        repository.update_rule(winner_id, expected_version=rule["version"], pattern="[")
    hint = repository.add_hint(
        "household", "Valid hint", pattern="hint", category_code="expense.food"
    )
    hint_version = next(item for item in repository.list_hints() if item["id"] == hint)["version"]
    with pytest.raises(ValueError, match="Invalid regular expression"):
        repository.update_hint(hint, expected_version=hint_version, pattern="[")
    repository.update_rule(winner_id, expected_version=rule["version"], active=False)
    assert (
        classification.find_rule(
            repository.get_transaction(transaction["id"]), "household"
        ).source_id
        == loser_id
    )
    updated = next(item for item in repository.list_rules() if item["id"] == winner_id)
    repository.delete_rule(winner_id, updated["version"])
    with pytest.raises(KeyError):
        repository.delete_rule(winner_id, updated["version"])
    with pytest.raises(VersionConflictError):
        repository.delete_rule(loser_id, 999)


def test_disabled_stages_allow_history_and_suppress_llm(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    first = _transaction(service, account_id, "history-first", "History merchant")
    repository.classify(
        first["id"],
        "household",
        "expense.food",
        "manual",
        1.0,
        "confirmed",
        expected_version=first["version"],
    )
    second = _transaction(service, account_id, "history-second", "History merchant")
    repository.add_rule("household", "Would win", "History merchant", "expense.children")
    pipeline = PipelineConfig(repository)
    pipeline.set_stage(account_id, "rule", False)
    identifier = ClassificationService(repository).classify_deterministic(second["id"], "household")
    assert identifier is not None
    applied = next(
        item
        for item in repository.list_classifications(status="applied")
        if item["transaction_id"] == second["id"]
    )
    assert applied["source"] == "history"

    third = _transaction(service, account_id, "llm-disabled", "No deterministic match")
    pipeline.set_stage(account_id, "llm", False)
    llm = CountingLLM()
    assert ClassificationService(repository, llm).classify(third["id"], "freelance") is None
    assert llm.calls == 0


def test_explain_shows_configured_llm_as_suggestion(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    transaction = _transaction(service, account_id, "llm-explanation", "No deterministic match")
    explanation = ClassificationService(repository, CountingLLM(), llm_model="test-model").explain(
        transaction["id"], "freelance"
    )
    assert explanation[-1] == {"stage": "llm", "status": "would_suggest", "model": "test-model"}


def test_rule_test_checks_older_transactions_with_bounded_results(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    _transaction(service, account_id, "older-match", "Older special match")
    for index in range(60):
        _transaction(service, account_id, f"newer-transaction-{index}", f"Other {index}")
    result = ClassificationService(repository).test_rule_pattern(
        "special", profile_code="household", account_id=account_id, limit=1000
    )
    assert result["examined"] == 61
    assert result["matches"][0]["purpose"] == "Older special match"


def test_classification_diagnostics_are_available_in_cli(tmp_path):
    repository, service, account_id = _setup(tmp_path)
    transaction = _transaction(service, account_id, "cli-explain-transaction", "CLI special")
    repository.add_rule("household", "CLI", "special", "expense.food")
    database_path = tmp_path / "ledger.db"
    assert (
        main(
            [
                "--db",
                str(database_path),
                "classification",
                "explain",
                transaction["id"],
                "--profile",
                "household",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--db",
                str(database_path),
                "classification",
                "test-rule",
                "--pattern",
                "special",
                "--profile",
                "household",
                "--account",
                account_id,
            ]
        )
        == 0
    )

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from dagoberd.config import (
    DeliveryConfig,
    EmailTargetConfig,
    LLMConfig,
    PrivacyConfig,
)
from dagoberd.ledger.classification import ClassificationService
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.llm.base import LLMClassificationInput, LLMClassificationSuggestion
from dagoberd.llm.openai_compatible import LLMProviderError
from dagoberd.profiles import install_builtin_profiles
from dagoberd.reporting.ledger_reports import LedgerReportGenerator
from dagoberd.secrets import SecretStore
from dagoberd.services.llm_preview import LLMPreviewService
from dagoberd.services.report_delivery import ReportDeliveryService


class FakeLLM:
    def __init__(self) -> None:
        self.items: list[LLMClassificationInput] = []

    def classify(self, item: LLMClassificationInput) -> LLMClassificationSuggestion:
        self.items.append(item)
        return LLMClassificationSuggestion(
            category="expense.business",
            confidence=0.8,
            rationale="Test",
            provider="fake",
            model="fake",
            prompt_version="test",
            prompt_hash="hash",
            taxonomy_version=item.taxonomy_version,
            duration_ms=1,
        )

    def test_connection(self) -> dict[str, str]:
        return {"status": "ok"}


class FailingLLM(FakeLLM):
    def classify(self, item: LLMClassificationInput) -> LLMClassificationSuggestion:
        self.items.append(item)
        raise TimeoutError("synthetic timeout")


class EmptySecrets(SecretStore):
    def get(self, name: str) -> str | None:
        return None


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.messages = []
        self.started_tls = False
        self.__class__.instances.append(self)

    def starttls(self, **_kwargs) -> None:
        self.started_tls = True

    def login(self, _username: str, _password: str) -> None:
        raise AssertionError("No credentials configured")

    def send_message(self, message) -> None:
        self.messages.append(message)

    def quit(self) -> None:
        pass


@pytest.fixture
def repository(tmp_path) -> LedgerRepository:
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    install_builtin_profiles(repo)
    repo.import_transactions(
        "file",
        "test",
        [
            NormalizedTransaction(
                "external-1",
                date(2026, 7, 1),
                Decimal("-12.50"),
                "EUR",
                "Mystery DE02120300000000202051",
                "account-1",
                counterparty="Vendor",
            )
        ],
    )
    return repo


def test_llm_preview_sends_only_exact_redacted_payload(repository):
    fake = FakeLLM()
    config = LLMConfig(
        enabled=True,
        mode="cloud",
        allow_cloud=True,
        model="cloud-test",
        base_url="https://example.invalid/v1",
    )
    transaction_id = repository.list_transactions()[0]["id"]
    service = LLMPreviewService(repository, fake, config, PrivacyConfig())

    preview = service.create("freelance", transaction_ids=[transaction_id])
    assert "DE02120300000000202051" not in str(preview["payloads"])
    assert fake.items == []

    result = service.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert result["status"] == "executed"
    assert fake.items[0].model_dump(mode="json") == preview["payloads"][0]
    assert repository.list_classifications(status="suggested")[0]["source"] == "llm"
    assert service.execute(str(preview["preview_id"]), str(preview["fingerprint"])) == result


def test_llm_preview_rejects_wrong_fingerprint(repository):
    fake = FakeLLM()
    config = LLMConfig(enabled=True, model="local-test")
    service = LLMPreviewService(repository, fake, config, PrivacyConfig())
    preview = service.create("freelance")
    with pytest.raises(ValueError, match="fingerprint"):
        service.execute(str(preview["preview_id"]), "wrong")
    assert fake.items == []


def test_failed_llm_preview_cannot_be_resent_implicitly(repository):
    fake = FailingLLM()
    config = LLMConfig(enabled=True, model="local-test")
    service = LLMPreviewService(repository, fake, config, PrivacyConfig())
    preview = service.create("freelance")
    with pytest.raises(TimeoutError, match="synthetic"):
        service.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert service.get(str(preview["preview_id"]))["status"] == "failed"
    with pytest.raises(ValueError, match="not executable"):
        service.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert len(fake.items) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://127.0.0.1:1234/v1"},
        {"model": "another-model"},
        {"temperature": 0.5},
        {"max_tokens": 1024},
        {"api_key_secret": "ANOTHER_SECRET_REFERENCE"},
    ],
)
def test_llm_preview_rejects_changed_request_configuration(repository, changes):
    fake = FakeLLM()
    config = LLMConfig(enabled=True, model="local-test")
    service = LLMPreviewService(repository, fake, config, PrivacyConfig())
    preview = service.create("freelance")
    changed = LLMPreviewService(
        repository, fake, config.model_copy(update=changes), PrivacyConfig()
    )
    with pytest.raises(ValueError, match="configuration changed"):
        changed.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert fake.items == []
    assert changed.get(str(preview["preview_id"]))["status"] == "invalidated"
    new_preview = changed.create("freelance")
    assert new_preview["fingerprint"] != preview["fingerprint"]
    assert new_preview["request_context"]["llm"]["base_url"] == changed.config.base_url


def test_llm_preview_rejects_changed_privacy_configuration(repository):
    fake = FakeLLM()
    config = LLMConfig(enabled=True, model="local-test")
    service = LLMPreviewService(repository, fake, config, PrivacyConfig())
    preview = service.create("freelance")
    changed = LLMPreviewService(repository, fake, config, PrivacyConfig(max_text_length=100))
    with pytest.raises(ValueError, match="configuration changed"):
        changed.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert fake.items == []


@pytest.mark.parametrize("expired", [True, False])
def test_llm_preview_checks_expiry_and_transaction_version_before_sending(repository, expired):
    fake = FakeLLM()
    service = LLMPreviewService(
        repository, fake, LLMConfig(enabled=True, model="test"), PrivacyConfig()
    )
    preview = service.create("freelance", ttl_minutes=-1 if expired else 30)
    if not expired:
        transaction = repository.list_transactions()[0]
        repository.classify(transaction["id"], "freelance", "expense.business", "manual", 1, "Test")
    with pytest.raises(ValueError, match=r"expired|changed after"):
        service.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert fake.items == []
    assert service.get(str(preview["preview_id"]))["status"] == (
        "expired" if expired else "invalidated"
    )


def test_invalid_model_output_is_visible_in_direct_and_preview_classification(repository):
    class InvalidLLM(FakeLLM):
        def classify(self, item):
            raise LLMProviderError("LLM returned invalid structured output")

    fake = InvalidLLM()
    transaction_id = repository.list_transactions()[0]["id"]
    with pytest.raises(LLMProviderError, match="invalid structured"):
        ClassificationService(repository, fake).classify(transaction_id, "freelance")
    assert repository.list_classifications(status="suggested") == []
    service = LLMPreviewService(
        repository, fake, LLMConfig(enabled=True, model="test"), PrivacyConfig()
    )
    preview = service.create("freelance")
    with pytest.raises(LLMProviderError, match="invalid structured"):
        service.execute(str(preview["preview_id"]), str(preview["fingerprint"]))
    assert service.get(str(preview["preview_id"]))["status"] == "failed"
    assert repository.list_classifications(status="suggested") == []


def test_report_delivery_requires_preview_and_is_idempotent(repository, tmp_path):
    LedgerReportGenerator(repository, tmp_path / "reports").generate(formats=("json",))
    config = DeliveryConfig(
        enabled=True,
        host="localhost",
        starttls=True,
        from_address="sender@example.org",
        targets={
            "personal": EmailTargetConfig(recipients=("owner@example.org",)),
        },
    )
    FakeSMTP.instances.clear()
    service = ReportDeliveryService(
        repository,
        config,
        EmptySecrets(),
        smtp_factory=FakeSMTP,
    )

    preview = service.preview("personal")
    assert preview["recipients"] == ["o***@example.org"]
    result = service.send(
        str(preview["preview_id"]),
        str(preview["fingerprint"]),
        idempotency_key="personal-test-1",
    )
    assert result["status"] == "sent"
    assert FakeSMTP.instances[0].started_tls is True
    assert len(FakeSMTP.instances[0].messages) == 1
    repeated = service.send(
        str(preview["preview_id"]),
        str(preview["fingerprint"]),
        idempotency_key="personal-test-1",
    )
    assert repeated == result
    assert len(FakeSMTP.instances) == 1

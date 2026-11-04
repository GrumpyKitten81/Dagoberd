from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from account_eval.config import LLMConfig, PrivacyConfig
from account_eval.llm.base import LLMClassificationInput
from account_eval.llm.openai_compatible import (
    LLMProviderError,
    OpenAICompatibleProvider,
    _parse_structured_content,
)
from account_eval.llm.redaction import redact_text


class FakeCompletions:
    def __init__(self, content: str, failures: int = 0) -> None:
        self.content = content
        self.failures = failures
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("test timeout")
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def _client(content: str, failures: int = 0):
    completions = FakeCompletions(content, failures)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        models=SimpleNamespace(list=lambda: SimpleNamespace(data=[])),
    ), completions


def _input():
    return LLMClassificationInput(
        transaction_id="transaction-1",
        booking_date="2026-07-01",
        amount="-10.00",
        currency="EUR",
        purpose="Software",
        profile="freelance",
        allowed_categories=("expense.business",),
        taxonomy_version="tax-v1",
    )


def test_local_openai_compatible_adapter_retries_and_validates_json():
    client, completions = _client(
        '{"category":"expense.business","confidence":0.7,"rationale":"Software"}',
        failures=1,
    )
    provider = OpenAICompatibleProvider(
        LLMConfig(enabled=True, model="local", max_retries=2),
        client=client,
    )
    suggestion = provider.classify(_input())
    assert suggestion.category == "expense.business"
    assert suggestion.prompt_hash
    assert completions.calls == 2


def test_adapter_rejects_category_outside_taxonomy():
    client, _ = _client('{"category":"income.freelance","confidence":0.7,"rationale":"x"}')
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    with pytest.raises(LLMProviderError, match="outside"):
        provider.classify(_input())


def test_cloud_mode_requires_opt_in_and_key():
    with pytest.raises(ValidationError, match="allow_cloud"):
        LLMConfig(enabled=True, mode="cloud", model="cloud")
    client, _ = _client("null")
    with pytest.raises(ValueError, match="API-key"):
        OpenAICompatibleProvider(
            LLMConfig(
                enabled=True,
                mode="cloud",
                model="cloud",
                allow_cloud=True,
            ),
            client=client,
        )


def test_parse_structured_content_handles_markdown_fences():
    payload = '```json\n{"category":"income.rent","confidence":0.75,"rationale":"Miete"}\n```'
    parsed = _parse_structured_content(payload)
    assert parsed == {
        "category": "income.rent",
        "confidence": 0.75,
        "rationale": "Miete",
    }


def test_parse_structured_content_rejects_chat_template_suffix():
    payload = '{"category":"expense.business","confidence":0.7,"rationale":"Software"}<|im_end|>\n'
    with pytest.raises(LLMProviderError, match="invalid structured"):
        _parse_structured_content(payload)


def test_adapter_parses_markdown_wrapped_json():
    client, _ = _client(
        '```json\n{"category":"expense.business","confidence":0.7,"rationale":"Software"}\n```'
    )
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    suggestion = provider.classify(_input())
    assert suggestion.category == "expense.business"


def test_parse_structured_content_rejects_truncated_json():
    payload = (
        '{"category": "legacy.Mietzahlung", "confidence": 0.9, '
        '"rationale": "Der Betrag-erhält-erhält'
    )
    with pytest.raises(LLMProviderError, match="invalid structured"):
        _parse_structured_content(payload)


def test_adapter_accepts_null_category():
    client, _ = _client('{"category":null,"confidence":0.0,"rationale":"unclear"}')
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    assert provider.classify(_input()) is None


def test_redaction_removes_identifiers_and_limits_text():
    privacy = PrivacyConfig(max_text_length=50)
    result = redact_text(
        "DE02120300000000202051 foo@example.org 123456789 extra",
        privacy,
    )
    assert "DE02120300000000202051" not in result
    assert "foo@example.org" not in result
    assert "123456789" not in result
    assert len(result) <= 50


@pytest.mark.parametrize(
    "changes",
    [
        {"rationale": None},
        {"rationale": "   "},
        {"rationale": 123},
        {"rationale": "x" * 1001},
        {"confidence": "0.7"},
        {"confidence": True},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"category": ""},
        {"category": 123},
        {"rationale_extra": "unexpected"},
    ],
)
def test_adapter_rejects_invalid_response_fields_without_retry(changes):
    payload = {"category": "expense.business", "confidence": 0.7, "rationale": "Software"}
    payload.update(changes)
    client, completions = _client(json.dumps(payload))
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    with pytest.raises(LLMProviderError, match="invalid structured"):
        provider.classify(_input())
    assert completions.calls == 1


@pytest.mark.parametrize("category", ["expense.business", None])
@pytest.mark.parametrize("field", ["category", "confidence", "rationale"])
def test_adapter_requires_all_fields_even_for_abstention(category, field):
    payload = {"category": category, "confidence": 0.7, "rationale": "Review"}
    del payload[field]
    client, _ = _client(json.dumps(payload))
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    with pytest.raises(LLMProviderError, match="invalid structured"):
        provider.classify(_input())


@pytest.mark.parametrize("content", ["", "null", "[]", '{"category": "expense.business"'])
def test_adapter_rejects_empty_or_incomplete_output(content):
    client, completions = _client(content)
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    with pytest.raises(LLMProviderError, match="structured output"):
        provider.classify(_input())
    assert completions.calls == 1


def test_adapter_rejects_truncation_even_if_content_is_complete_json():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content='{"category":null,"confidence":0,"rationale":"x"}'),
            )
        ],
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    provider = OpenAICompatibleProvider(LLMConfig(enabled=True, model="local"), client=client)
    with pytest.raises(LLMProviderError, match="incomplete structured"):
        provider.classify(_input())

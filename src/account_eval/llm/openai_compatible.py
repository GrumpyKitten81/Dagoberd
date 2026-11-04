"""OpenAI-compatible local/cloud adapter with strict JSON output."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from openai import APIConnectionError, APIError, APIStatusError, DefaultHttpxClient, OpenAI
from pydantic import ValidationError

from account_eval.config import LLMConfig

from .base import LLMClassificationInput, LLMClassificationResponse, LLMClassificationSuggestion

PROMPT_VERSION = "account-eval-classification-v3"


class LLMProviderError(RuntimeError):
    """The configured LLM could not return a valid structured suggestion."""


def _parse_structured_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[len("```json\n") : -len("\n```")]
    elif text.startswith("```\n") and text.endswith("\n```"):
        text = text[len("```\n") : -len("\n```")]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise LLMProviderError("LLM returned invalid structured output") from error
    if not isinstance(parsed, dict):
        raise LLMProviderError("LLM returned invalid structured output")
    return parsed


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: LLMConfig,
        api_key: str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        config = LLMConfig.model_validate(config.model_dump())
        if not config.enabled:
            raise ValueError("LLM is disabled in account-eval.toml")
        if config.mode == "cloud" and not config.allow_cloud:
            raise ValueError("Cloud LLM use requires llm.allow_cloud = true")
        if config.mode == "cloud" and not api_key:
            raise ValueError("Cloud LLM use requires the configured API-key secret")
        self.config = config
        if client is None:
            client = OpenAI(
                base_url=config.base_url,
                api_key=api_key or "local-no-key",
                timeout=config.timeout_seconds,
                max_retries=0,
                http_client=DefaultHttpxClient(
                    follow_redirects=False,
                    trust_env=config.mode != "local",
                ),
            )
        self.client = client

    @staticmethod
    def _prompt(item: LLMClassificationInput) -> tuple[str, str]:
        payload = item.model_dump(mode="json")
        prompt = (
            "Classify exactly one financial transaction. Respond only as a "
            "JSON object with category, confidence and rationale. category must be "
            "an exact member of allowed_categories. If no category is justified, "
            "set category to null; confidence and rationale remain required. "
            "Do not invent or infer missing data.\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        digest = hashlib.sha256(f"{PROMPT_VERSION}\n{prompt}".encode()).hexdigest()
        return prompt, digest

    def classify(
        self,
        item: LLMClassificationInput,
    ) -> LLMClassificationSuggestion | None:
        prompt, prompt_hash = self._prompt(item)
        started = time.monotonic()
        LLMConfig.model_validate(self.config.model_dump())
        for _attempt in range(self.config.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": "Du bist ein vorsichtiger Finanzklassifikator.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                )
                break
            except (APIError, TimeoutError) as error:
                retryable = isinstance(error, (APIConnectionError, TimeoutError)) or (
                    isinstance(error, APIStatusError)
                    and (error.status_code == 429 or error.status_code >= 500)
                )
                if not retryable or _attempt == self.config.max_retries:
                    raise LLMProviderError("LLM request failed") from error
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise LLMProviderError("LLM returned incomplete structured output")
        content = response.choices[0].message.content
        if not content:
            raise LLMProviderError("LLM returned empty structured output")
        try:
            parsed = LLMClassificationResponse.model_validate(_parse_structured_content(content))
        except ValidationError as error:
            raise LLMProviderError("LLM returned invalid structured output") from error
        if parsed.category is None:
            return None
        if parsed.category not in item.allowed_categories:
            raise LLMProviderError("LLM returned a category outside the active taxonomy")
        return LLMClassificationSuggestion(
            category=parsed.category,
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            provider=self.config.provider,
            model=self.config.model,
            prompt_version=PROMPT_VERSION,
            prompt_hash=prompt_hash,
            taxonomy_version=item.taxonomy_version,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def test_connection(self) -> dict[str, str]:
        LLMConfig.model_validate(self.config.model_dump())
        try:
            models = self.client.models.list()
            count = len(getattr(models, "data", []) or [])
        except (APIError, TimeoutError) as error:
            raise LLMProviderError("LLM connection test failed") from error
        return {"status": "ok", "model": self.config.model, "models_visible": str(count)}

"""Create configured LLM providers without leaking secrets."""

from __future__ import annotations

from account_eval.config import AppConfig
from account_eval.secrets import SecretStore

from .base import LLMProvider
from .openai_compatible import OpenAICompatibleProvider


def create_llm_provider(
    config: AppConfig,
    secrets: SecretStore,
) -> LLMProvider | None:
    if not config.llm.enabled:
        return None
    if config.llm.provider != "openai-compatible":
        raise ValueError(f"Unsupported LLM provider: {config.llm.provider}")
    api_key = secrets.get(config.llm.api_key_secret)
    return OpenAICompatibleProvider(config.llm, api_key)

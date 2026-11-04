"""Provider-neutral contracts for structured LLM classification."""

from __future__ import annotations

from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class LLMClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: Annotated[str, StringConstraints(min_length=1)] | None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    rationale: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]


class LLMClassificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: str
    booking_date: str
    amount: str
    currency: str
    purpose: str
    counterparty: str | None = None
    reference: str | None = None
    profile: str
    allowed_categories: tuple[str, ...]
    hints: tuple[str, ...] = ()
    taxonomy_version: str


class LLMClassificationSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=1000)
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    taxonomy_version: str
    duration_ms: int = Field(ge=0)


class LLMProvider(Protocol):
    def classify(
        self,
        item: LLMClassificationInput,
    ) -> LLMClassificationSuggestion | None: ...

    def test_connection(self) -> dict[str, str]: ...

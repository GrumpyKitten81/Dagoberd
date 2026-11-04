"""Validated API request payloads."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VersionedRequest(BaseModel):
    expected_version: int = Field(ge=1)


class CategoryRequest(VersionedRequest):
    profile_code: str
    category_code: str


class TagRequest(VersionedRequest):
    tag: str
    remove: bool = False


class DimensionRequest(VersionedRequest):
    dimension_code: str
    value_code: str | None = None


class MerchantRequest(VersionedRequest):
    merchant: str


class NoteRequest(VersionedRequest):
    note: str = Field(max_length=2000)


class StatusRequest(VersionedRequest):
    status: str


class AccountOptionsRequest(BaseModel):
    field_kind: str
    codes: list[str]


class ManualTransactionRequest(BaseModel):
    account_id: str
    booking_date: str
    amount: Decimal
    purpose: str
    valuta_date: str | None = None
    counterparty: str | None = None
    reference: str | None = None
    booking_status: str = "booked"


class ManualTransactionUpdateRequest(VersionedRequest):
    booking_date: str | None = None
    amount: Decimal | None = None
    purpose: str | None = None
    valuta_date: str | None = None
    counterparty: str | None = None
    reference: str | None = None


class DocumentAssignRequest(BaseModel):
    transaction_id: str
    expected_document_version: int | None = Field(default=None, ge=1)
    expected_transaction_version: int | None = Field(default=None, ge=1)
    append: bool = False


class DocumentBatchRunRequest(BaseModel):
    fingerprint: str


class DocumentMatchApplyRequest(BaseModel):
    fingerprint: str
    transaction_ids: list[str]
    expected_document_version: int = Field(ge=1)
    append: bool = True


class MatchReviewRequest(VersionedRequest):
    decision: str


class SplitPart(BaseModel):
    amount: Decimal
    category_code: str | None = None
    comment: str | None = None


class SplitRequest(VersionedRequest):
    profile_code: str
    parts: list[SplitPart]


class BulkPreviewRequest(BaseModel):
    action: str
    transaction_ids: list[str] = Field(min_length=1, max_length=500)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_composite_patch(self) -> BulkPreviewRequest:
        if self.action == "edit_fields":
            self.payload = BulkEditFieldsPatch.model_validate(self.payload).model_dump(
                exclude_none=True
            )
        return self


class StrictPatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BulkCategoryPatch(StrictPatchModel):
    profile: str
    category: str


class BulkTagsPatch(StrictPatchModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class BulkModePatch(StrictPatchModel):
    mode: Literal["set", "clear"]
    value: str | None = None


class BulkNotePatch(StrictPatchModel):
    mode: Literal["replace", "append", "clear"]
    text: str | None = Field(default=None, max_length=2000)


class BulkStatusPatch(StrictPatchModel):
    value: str


class BulkEditFieldsPatch(StrictPatchModel):
    category: BulkCategoryPatch | None = None
    tags: BulkTagsPatch | None = None
    merchant: BulkModePatch | None = None
    dimensions: dict[str, BulkModePatch] | None = None
    note: BulkNotePatch | None = None
    status: BulkStatusPatch | None = None


class BulkContextRequest(BaseModel):
    transaction_ids: list[str] = Field(min_length=1, max_length=500)


class BulkRunRequest(BaseModel):
    fingerprint: str | None = None

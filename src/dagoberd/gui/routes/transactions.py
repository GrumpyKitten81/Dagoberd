"""Transaction reads and mutations."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Query, Request

from dagoberd.accounts import AccountScope
from dagoberd.contracts import GUI_CONTRACT_VERSION, success
from dagoberd.gui.context import GuiContext
from dagoberd.gui.schemas import (
    BulkContextRequest,
    BulkPreviewRequest,
    BulkRunRequest,
    CategoryRequest,
    DimensionRequest,
    ManualTransactionRequest,
    ManualTransactionUpdateRequest,
    MerchantRequest,
    NoteRequest,
    SplitRequest,
    StatusRequest,
    TagRequest,
)
from dagoberd.ledger.models import TransactionSearchFilter, to_minor_units

router = APIRouter(prefix="/api")
ACCOUNT_QUERY: list[str] = Query(default=[])
CURRENCY_QUERY: list[str] = Query(default=[])
STATUS_QUERY: list[str] = Query(default=[])
CATEGORY_QUERY: list[str] = Query(default=[])
TAG_QUERY: list[str] = Query(default=[])
DIMENSION_QUERY: list[str] = Query(default=[])


def _context(request: Request) -> GuiContext:
    return request.app.state.context


def _filters(
    text: str | None,
    booking_from: str | None,
    booking_to: str | None,
    amount_min: str | None,
    amount_max: str | None,
    currency: list[str],
    booking_status: list[str],
    category_code: list[str],
    tag: list[str],
    dimension: list[str],
    merchant: str | None,
    document_status: str | None,
    only_unreviewed: bool,
) -> TransactionSearchFilter:
    dimensions: dict[str, tuple[str, ...]] = {}
    for item in dimension:
        code, separator, value = item.partition(":")
        if not separator or not code or not value:
            raise ValueError("dimension must use code:value")
        dimensions[code] = tuple((*dimensions.get(code, ()), value))
    return TransactionSearchFilter(
        text=text,
        booking_from=booking_from,
        booking_to=booking_to,
        amount_min_minor=to_minor_units(Decimal(amount_min)) if amount_min else None,
        amount_max_minor=to_minor_units(Decimal(amount_max)) if amount_max else None,
        currencies=tuple(currency),
        booking_statuses=tuple(booking_status),
        category_codes=tuple(category_code),
        tags=tuple(tag),
        dimensions=dimensions,
        merchant=merchant,
        document_status=document_status,
        only_unreviewed=only_unreviewed,
    )


@router.get("/transactions")
def transactions(
    request: Request,
    account_id: list[str] = ACCOUNT_QUERY,
    text: str | None = None,
    booking_from: str | None = None,
    booking_to: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    currency: list[str] = CURRENCY_QUERY,
    booking_status: list[str] = STATUS_QUERY,
    category_code: list[str] = CATEGORY_QUERY,
    tag: list[str] = TAG_QUERY,
    dimension: list[str] = DIMENSION_QUERY,
    merchant: str | None = None,
    document_status: str | None = None,
    only_unreviewed: bool = False,
    limit: int = 100,
    offset: int = 0,
    sort_by: Literal["date", "amount", "account", "counterparty", "category"] = "date",
    sort_direction: Literal["asc", "desc"] = "desc",
) -> dict:
    filters = _filters(
        text,
        booking_from,
        booking_to,
        amount_min,
        amount_max,
        currency,
        booking_status,
        category_code,
        tag,
        dimension,
        merchant,
        document_status,
        only_unreviewed,
    )
    scope = AccountScope.selected(account_id) if account_id else AccountScope.all()
    organization = _context(request).organization()
    return success(
        {
            "items": organization.search_transactions(
                filters,
                account_scope=scope,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                sort_direction=sort_direction,
            ),
            "total": organization.count_transactions(filters, account_scope=scope),
            "limit": limit,
            "offset": offset,
        },
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/transactions/{transaction_id}")
def transaction(transaction_id: str, request: Request) -> dict:
    return success(
        _context(request).repository().transaction_detail(transaction_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.get("/transactions/{transaction_id}/row")
def transaction_row(transaction_id: str, request: Request) -> dict:
    return success(
        _context(request).organization().transaction_search_record(transaction_id),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/category")
def category(transaction_id: str, payload: CategoryRequest, request: Request) -> dict:
    repository = _context(request).repository()
    classification_id = repository.classify(
        transaction_id,
        payload.profile_code,
        payload.category_code,
        "manual",
        1.0,
        "GUI edit",
        "applied",
        payload.expected_version,
    )
    return success(
        {
            "transaction_id": transaction_id,
            "classification_id": classification_id,
            "version": repository.get_transaction(transaction_id)["version"],
            "changed": True,
        },
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/tags")
def tags(transaction_id: str, payload: TagRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .tag_transaction(
            transaction_id,
            payload.tag,
            expected_version=payload.expected_version,
            remove=payload.remove,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/dimensions")
def dimensions(transaction_id: str, payload: DimensionRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .set_transaction_dimension(
            transaction_id,
            payload.dimension_code,
            payload.value_code,
            expected_version=payload.expected_version,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/merchant")
def merchant(transaction_id: str, payload: MerchantRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .set_transaction_merchant(
            transaction_id, payload.merchant, expected_version=payload.expected_version
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/note")
def note(transaction_id: str, payload: NoteRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .set_transaction_note(
            transaction_id, payload.note, expected_version=payload.expected_version
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/status")
def status(transaction_id: str, payload: StatusRequest, request: Request) -> dict:
    version = (
        _context(request)
        .repository()
        .transition_transaction_status(
            transaction_id,
            payload.status,
            payload.expected_version,
            reason="GUI edit",
            source="gui",
        )
    )
    return success(
        {
            "transaction_id": transaction_id,
            "version": version,
            "changed": version != payload.expected_version,
        },
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions")
def create_manual(payload: ManualTransactionRequest, request: Request) -> dict:
    result = (
        _context(request)
        .organization()
        .create_manual_transaction(
            payload.account_id,
            booking_date=date.fromisoformat(payload.booking_date),
            valuta_date=date.fromisoformat(payload.valuta_date) if payload.valuta_date else None,
            amount=payload.amount,
            purpose=payload.purpose,
            counterparty=payload.counterparty,
            reference=payload.reference,
            booking_status=payload.booking_status,
            idempotency_key=str(uuid4()),
        )
    )
    return success(result, contract_version=GUI_CONTRACT_VERSION)


@router.patch("/transactions/{transaction_id}")
def update_manual(
    transaction_id: str, payload: ManualTransactionUpdateRequest, request: Request
) -> dict:
    return success(
        _context(request)
        .organization()
        .update_manual_transaction(
            transaction_id,
            expected_version=payload.expected_version,
            booking_date=date.fromisoformat(payload.booking_date) if payload.booking_date else None,
            valuta_date=date.fromisoformat(payload.valuta_date) if payload.valuta_date else None,
            amount=payload.amount,
            purpose=payload.purpose,
            counterparty=payload.counterparty,
            reference=payload.reference,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/transactions/{transaction_id}/splits")
def split(transaction_id: str, payload: SplitRequest, request: Request) -> dict:
    identifiers = (
        _context(request)
        .repository()
        .split_transaction(
            transaction_id,
            payload.profile_code,
            [(part.amount, part.category_code, part.comment) for part in payload.parts],
            payload.expected_version,
        )
    )
    return success(
        {"transaction_id": transaction_id, "split_ids": identifiers},
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.put("/transactions/{transaction_id}/splits")
def replace_split(transaction_id: str, payload: SplitRequest, request: Request) -> dict:
    identifiers = (
        _context(request)
        .repository()
        .replace_split(
            transaction_id,
            payload.profile_code,
            [(part.amount, part.category_code, part.comment) for part in payload.parts],
            payload.expected_version,
        )
    )
    return success(
        {"transaction_id": transaction_id, "split_ids": identifiers},
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.delete("/transactions/{transaction_id}/splits")
def revert_split(transaction_id: str, expected_version: int, request: Request) -> dict:
    _context(request).repository().revert_split(transaction_id, expected_version)
    return success(
        {"transaction_id": transaction_id, "changed": True}, contract_version=GUI_CONTRACT_VERSION
    )


@router.post("/bulk/preview")
def bulk_preview(payload: BulkPreviewRequest, request: Request) -> dict:
    return success(
        _context(request)
        .organization()
        .create_bulk_preview(payload.action, payload.transaction_ids, payload.payload),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/bulk/context")
def bulk_context(payload: BulkContextRequest, request: Request) -> dict:
    return success(
        _context(request).organization().bulk_edit_context(payload.transaction_ids),
        contract_version=GUI_CONTRACT_VERSION,
    )


@router.post("/bulk/{preview_id}/run")
def run_bulk(preview_id: str, request: Request, payload: BulkRunRequest | None = None) -> dict:
    return success(
        _context(request)
        .organization()
        .execute_bulk_preview(
            preview_id,
            fingerprint=payload.fingerprint if payload is not None else None,
        ),
        contract_version=GUI_CONTRACT_VERSION,
    )

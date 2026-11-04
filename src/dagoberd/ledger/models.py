"""Stable, provider-neutral models used by imports and providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any


def to_minor_units(amount: Decimal, exponent: int = 2) -> int:
    """Convert a Decimal amount without ever passing through float."""
    factor = Decimal(10) ** exponent
    return int((amount * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor_units(amount_minor: int, exponent: int = 2) -> Decimal:
    return Decimal(amount_minor) / (Decimal(10) ** exponent)


class TransactionStatus(StrEnum):
    """Provider-neutral lifecycle of one normalized bank booking."""

    PENDING = "pending"
    BOOKED = "booked"
    REVERSED = "reversed"
    CORRECTED = "corrected"


class ImportRowKind(StrEnum):
    """Semantic kind of a source row before it reaches the ledger."""

    TRANSACTION = "transaction"
    OPENING_BALANCE = "opening_balance"


class TransactionRelationType(StrEnum):
    """Explicit semantic links between otherwise immutable transactions."""

    TRANSFER = "transfer"
    REFUND = "refund"
    REVERSAL = "reversal"
    CORRECTION = "correction"


class TransactionRelationStatus(StrEnum):
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class AccountType(StrEnum):
    CHECKING = "checking"
    CREDIT_CARD = "credit_card"
    SAVINGS = "savings"
    CASH = "cash"
    LOAN = "loan"
    ASSET = "asset"
    LIABILITY = "liability"


class BulkAction(StrEnum):
    CATEGORIZE = "categorize"
    TAG_ADD = "tag_add"
    TAG_REMOVE = "tag_remove"
    DIMENSION_SET = "dimension_set"
    DIMENSION_CLEAR = "dimension_clear"
    MERCHANT_SET = "merchant_set"
    EDIT_FIELDS = "edit_fields"


VALID_TRANSACTION_STATUS_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.PENDING: frozenset({TransactionStatus.BOOKED, TransactionStatus.REVERSED}),
    TransactionStatus.BOOKED: frozenset({TransactionStatus.REVERSED, TransactionStatus.CORRECTED}),
    TransactionStatus.REVERSED: frozenset(),
    TransactionStatus.CORRECTED: frozenset(),
}


def transaction_status(value: str | TransactionStatus) -> TransactionStatus:
    try:
        return TransactionStatus(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in TransactionStatus)
        raise ValueError(
            f"Unknown transaction status {value!r}; expected one of {allowed}"
        ) from error


def account_type(value: str | AccountType) -> AccountType:
    try:
        return AccountType(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in AccountType)
        raise ValueError(f"Unknown account type {value!r}; expected one of {allowed}") from error


@dataclass(frozen=True, slots=True)
class TransactionSearchFilter:
    """Composable, provider-neutral transaction search contract."""

    text: str | None = None
    booking_from: str | None = None
    booking_to: str | None = None
    amount_min_minor: int | None = None
    amount_max_minor: int | None = None
    currencies: tuple[str, ...] = ()
    booking_statuses: tuple[str, ...] = ()
    category_codes: tuple[str, ...] = ()
    classification_sources: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    dimensions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    merchant: str | None = None
    document_status: str | None = None
    only_unreviewed: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedTransaction:
    external_id: str
    booking_date: date
    amount: Decimal
    currency: str
    purpose: str
    account_external_id: str
    valuta_date: date | None = None
    counterparty: str | None = None
    reference: str | None = None
    note: str | None = None
    source_attributes: dict[str, Any] = field(default_factory=dict)
    observed_balance: Decimal | None = None
    row_kind: ImportRowKind | str = ImportRowKind.TRANSACTION
    raw: dict[str, Any] | None = None
    booking_status: TransactionStatus | str = TransactionStatus.BOOKED

    def __post_init__(self) -> None:
        transaction_status(self.booking_status)
        ImportRowKind(self.row_kind)


@dataclass(frozen=True, slots=True)
class ImportBalanceObservation:
    """One balance reported by a source row, retained for validation."""

    external_id: str
    account_external_id: str
    balance_date: date
    balance: Decimal
    currency: str
    source_row: int
    row_kind: ImportRowKind | str = ImportRowKind.TRANSACTION
    source_attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        ImportRowKind(self.row_kind)


@dataclass(frozen=True, slots=True)
class ImportBalanceSnapshot:
    """Operational balance selected from immutable source observations."""

    external_id: str
    account_external_id: str
    balance_date: date
    balance: Decimal
    currency: str
    roles: tuple[str, ...]
    source_row: int
    source_attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported: int
    skipped: int
    accounts: int
    updated: int = 0
    balance_snapshots_created: int = 0
    balance_snapshots_skipped: int = 0
    annotations_applied: int = 0
    transfer_candidates: int = 0
    transfer_relations_suggested: int = 0


@dataclass(frozen=True, slots=True)
class ImportCategoryAnnotation:
    """One profile/category assignment prepared before an atomic import."""

    external_id: str
    account_external_id: str
    profile_code: str
    category_code: str
    category_name: str

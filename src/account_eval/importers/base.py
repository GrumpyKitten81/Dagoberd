from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from account_eval.ledger.models import (
    ImportBalanceObservation,
    ImportBalanceSnapshot,
    ImportRowKind,
    NormalizedTransaction,
)


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row: int | None
    field: str | None
    code: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImportBatch:
    transactions: tuple[NormalizedTransaction, ...]
    issues: tuple[ImportIssue, ...] = ()
    rows_total: int = 0
    balance_observations: tuple[ImportBalanceObservation, ...] = ()
    balance_snapshots: tuple[ImportBalanceSnapshot, ...] = ()


def select_balance_snapshots(
    observations: Iterable[ImportBalanceObservation],
) -> tuple[ImportBalanceSnapshot, ...]:
    """Select opening, month-end and closing snapshots for every account/currency."""

    grouped: dict[tuple[str, str], list[ImportBalanceObservation]] = {}
    for observation in observations:
        grouped.setdefault((observation.account_external_id, observation.currency), []).append(
            observation
        )

    selected: list[ImportBalanceSnapshot] = []
    for account_key in sorted(grouped):
        account_observations = sorted(
            grouped[account_key], key=lambda item: (item.balance_date, item.source_row)
        )
        roles_by_observation: dict[tuple[str, int], set[str]] = {}
        observations_by_key: dict[tuple[str, int], ImportBalanceObservation] = {}

        for observation in account_observations:
            key = (observation.external_id, observation.source_row)
            observations_by_key[key] = observation
            if ImportRowKind(observation.row_kind) is ImportRowKind.OPENING_BALANCE:
                roles_by_observation.setdefault(key, set()).add("opening")

        month_end: dict[tuple[int, int], ImportBalanceObservation] = {}
        for observation in account_observations:
            month_end[(observation.balance_date.year, observation.balance_date.month)] = observation
        for observation in month_end.values():
            key = (observation.external_id, observation.source_row)
            observations_by_key[key] = observation
            roles_by_observation.setdefault(key, set()).add("month_end")

        closing = account_observations[-1]
        closing_key = (closing.external_id, closing.source_row)
        observations_by_key[closing_key] = closing
        roles_by_observation.setdefault(closing_key, set()).add("closing")

        role_order = {"opening": 0, "month_end": 1, "closing": 2}
        for key, roles in roles_by_observation.items():
            observation = observations_by_key[key]
            selected.append(
                ImportBalanceSnapshot(
                    external_id=observation.external_id,
                    account_external_id=observation.account_external_id,
                    balance_date=observation.balance_date,
                    balance=observation.balance,
                    currency=observation.currency,
                    roles=tuple(sorted(roles, key=role_order.__getitem__)),
                    source_row=observation.source_row,
                    source_attributes=observation.source_attributes,
                    raw=observation.raw,
                )
            )
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.account_external_id,
                item.currency,
                item.balance_date,
                item.source_row,
            ),
        )
    )


class ImportValidationError(ValueError):
    def __init__(self, issues: Iterable[ImportIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(
            "; ".join(issue.message for issue in self.issues) or "Import validation failed"
        )


class FileImporter(ABC):
    source: str
    version = "1"
    provider_kind = "file"
    provider_name: str
    requires_account = False

    def inspect_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> ImportBatch:
        transactions = tuple(self.read_transactions(path, account_external_id))
        return ImportBatch(
            transactions=transactions,
            rows_total=len(transactions),
        )

    @abstractmethod
    def read_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> Iterable[NormalizedTransaction]:
        pass

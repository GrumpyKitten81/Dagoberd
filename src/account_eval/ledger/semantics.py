"""Explainable candidate generation for transaction relationships."""

from __future__ import annotations

import re
from datetime import date
from itertools import combinations

from account_eval.accounts import AccountScope

from .models import TransactionRelationType
from .repository import LedgerRepository


class TransactionSemanticsService:
    """Suggest relations without silently changing financial meaning."""

    SCORER_VERSION = "transaction-relations-v1"

    def __init__(
        self,
        repository: LedgerRepository,
        *,
        transfer_max_days: int = 5,
        transfer_max_fee_minor: int = 1000,
        refund_max_days: int = 90,
        correction_max_days: int = 14,
        minimum_score: float = 0.6,
    ) -> None:
        if transfer_max_days < 0 or refund_max_days < 0:
            raise ValueError("Candidate date tolerances must not be negative")
        if correction_max_days < 0 or transfer_max_fee_minor < 0:
            raise ValueError("Candidate tolerances must not be negative")
        if not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score must be between 0 and 1")
        self.repository = repository
        self.transfer_max_days = transfer_max_days
        self.transfer_max_fee_minor = transfer_max_fee_minor
        self.refund_max_days = refund_max_days
        self.correction_max_days = correction_max_days
        self.minimum_score = minimum_score

    def suggest(
        self,
        relation_type: str | TransactionRelationType,
        *,
        transaction_id: str | None = None,
        account_scope: AccountScope | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        kind = TransactionRelationType(relation_type)
        transactions = [
            item
            for item in self.repository.list_transactions(
                limit=None,
                account_scope=account_scope,
                booking_statuses=("booked",),
            )
            if item.get("split_id") is None and item["state"] == "active"
        ]
        ranked: list[tuple[float, dict, dict, int, list[str]]] = []
        for first, second in combinations(transactions, 2):
            if transaction_id and transaction_id not in {first["id"], second["id"]}:
                continue
            candidate = self._score(kind, first, second)
            if candidate is None:
                continue
            source, target, score, fee, reasons = candidate
            if score >= self.minimum_score:
                ranked.append((score, source, target, fee, reasons))
        ranked.sort(
            key=lambda item: (
                item[0],
                item[1]["booking_date"],
                item[1]["id"],
                item[2]["id"],
            ),
            reverse=True,
        )
        results: list[dict] = []
        for score, source, target, fee, reasons in ranked[:limit]:
            results.append(
                self.repository.create_transaction_relation(
                    kind,
                    source["id"],
                    target["id"],
                    expected_source_version=int(source["version"]),
                    expected_target_version=int(target["version"]),
                    score=score,
                    reasons=reasons,
                    fee_amount_minor=fee,
                    metadata={"scorer_version": self.SCORER_VERSION},
                )
            )
        return results

    def _score(
        self,
        kind: TransactionRelationType,
        first: dict,
        second: dict,
    ) -> tuple[dict, dict, float, int, list[str]] | None:
        if first["currency"] != second["currency"]:
            return None
        if kind is TransactionRelationType.TRANSFER:
            return self._score_transfer(first, second)
        older, newer = sorted(
            (first, second),
            key=lambda item: (item["booking_date"], item["id"]),
        )
        days = self._days(older, newer)
        if kind is TransactionRelationType.REFUND:
            return self._score_refund(older, newer, days)
        if kind is TransactionRelationType.REVERSAL:
            return self._score_reversal(older, newer, days)
        return self._score_correction(older, newer, days)

    def _score_transfer(
        self,
        first: dict,
        second: dict,
    ) -> tuple[dict, dict, float, int, list[str]] | None:
        source, target = (first, second) if first["amount_minor"] < 0 else (second, first)
        if source["amount_minor"] >= 0 or target["amount_minor"] <= 0:
            return None
        if source["account_id"] == target["account_id"]:
            return None
        days = abs(self._days(source, target))
        if days > self.transfer_max_days:
            return None
        fee = abs(int(source["amount_minor"])) - int(target["amount_minor"])
        if fee < 0 or fee > self.transfer_max_fee_minor:
            return None
        reasons = [
            "Opposite amounts on different own accounts",
            f"Buchungsabstand: {days} Tag(e)",
        ]
        score = 0.6 if fee == 0 else 0.45
        reasons.append(
            "Amounts match exactly" if fee == 0 else f"Detected transfer fee: {fee} Minor Units"
        )
        if days == 0:
            score += 0.25
        elif days <= 1:
            score += 0.2
        elif days <= 3:
            score += 0.1
        else:
            score += 0.05
        overlap = self._text_overlap(source, target)
        if overlap:
            score += 0.15
            reasons.append("Shared reference/purpose characteristics")
        return source, target, min(score, 1.0), fee, reasons

    def _score_refund(
        self,
        older: dict,
        newer: dict,
        days: int,
    ) -> tuple[dict, dict, float, int, list[str]] | None:
        if days < 0 or days > self.refund_max_days:
            return None
        if older["amount_minor"] >= 0 or newer["amount_minor"] <= 0:
            return None
        if newer["amount_minor"] > abs(older["amount_minor"]):
            return None
        exact = newer["amount_minor"] == abs(older["amount_minor"])
        score = 0.55 if exact else 0.35
        score += 0.2 if days <= 7 else 0.15 if days <= 30 else 0.05
        reasons = [
            "Later credit for an earlier expense",
            "Full refund" if exact else "Possible partial refund",
            f"Buchungsabstand: {days} Tag(e)",
        ]
        if self._text_overlap(older, newer):
            score += 0.25
            reasons.append("Shared merchant/reference characteristics")
        return older, newer, min(score, 1.0), 0, reasons

    def _score_reversal(
        self,
        older: dict,
        newer: dict,
        days: int,
    ) -> tuple[dict, dict, float, int, list[str]] | None:
        if days < 0 or days > self.correction_max_days:
            return None
        if older["account_id"] != newer["account_id"]:
            return None
        if older["amount_minor"] != -newer["amount_minor"]:
            return None
        score = 0.6 + (0.2 if days <= 3 else 0.1)
        reasons = [
            "Exact opposite amount on the same account",
            f"Buchungsabstand: {days} Tag(e)",
        ]
        if self._text_overlap(older, newer):
            score += 0.2
            reasons.append("Shared reference/purpose characteristics")
        return older, newer, min(score, 1.0), 0, reasons

    def _score_correction(
        self,
        older: dict,
        newer: dict,
        days: int,
    ) -> tuple[dict, dict, float, int, list[str]] | None:
        if days < 0 or days > self.correction_max_days:
            return None
        if older["account_id"] != newer["account_id"]:
            return None
        older_amount = int(older["amount_minor"])
        newer_amount = int(newer["amount_minor"])
        if older_amount == 0 or newer_amount == 0:
            return None
        if (older_amount < 0) != (newer_amount < 0):
            return None
        difference_ratio = abs(abs(older_amount) - abs(newer_amount)) / max(
            abs(older_amount),
            abs(newer_amount),
        )
        if difference_ratio == 0:
            score = 0.55
            amount_reason = "Same amount on the same account"
        elif difference_ratio <= 0.1:
            score = 0.4
            amount_reason = "Leicht abweichender Korrekturbetrag"
        elif difference_ratio <= 0.5:
            score = 0.25
            amount_reason = "Abweichender Korrekturbetrag"
        else:
            return None
        score += 0.2 if days <= 3 else 0.1
        reasons = [
            amount_reason,
            f"Buchungsabstand: {days} Tag(e)",
        ]
        if self._text_overlap(older, newer):
            score += 0.25
            reasons.append("Shared reference/purpose characteristics")
        return older, newer, min(score, 1.0), 0, reasons

    @staticmethod
    def _days(first: dict, second: dict) -> int:
        return (
            date.fromisoformat(second["booking_date"]) - date.fromisoformat(first["booking_date"])
        ).days

    @classmethod
    def _text_overlap(cls, first: dict, second: dict) -> bool:
        return bool(cls._tokens(first) & cls._tokens(second))

    @staticmethod
    def _tokens(transaction: dict) -> set[str]:
        text = " ".join(
            str(transaction.get(field) or "") for field in ("purpose", "counterparty", "reference")
        ).casefold()
        return {token for token in re.findall(r"[a-z0-9äöüß]+", text) if len(token) >= 4}

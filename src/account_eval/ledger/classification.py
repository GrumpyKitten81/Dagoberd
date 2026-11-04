"""Ordered, inspectable classification pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from account_eval.accounts import AccountScope
from account_eval.config import PrivacyConfig
from account_eval.llm import LLMClassificationInput, LLMProvider
from account_eval.llm.openai_compatible import LLMProviderError
from account_eval.llm.redaction import redact_text

from .models import from_minor_units
from .pipeline import PipelineConfig
from .repository import LedgerRepository


@dataclass(frozen=True, slots=True)
class StageResult:
    stage: str
    category_code: str
    confidence: float
    rationale: str
    source_id: str
    detail: dict[str, Any]


class ClassificationService:
    def __init__(
        self,
        repository: LedgerRepository,
        llm: LLMProvider | None = None,
        privacy: PrivacyConfig | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.privacy = privacy or PrivacyConfig()
        self.llm_model = llm_model
        self.pipeline = PipelineConfig(repository)

    def classify(self, transaction_id: str, profile_code: str) -> str | None:
        deterministic = self.classify_deterministic(transaction_id, profile_code)
        if deterministic or not self.stage_enabled(transaction_id, "llm") or self.llm is None:
            return deterministic
        item = self.build_llm_input(transaction_id, profile_code)
        if item is None:
            return None
        transaction = self._transaction(transaction_id)
        try:
            suggestion = self.llm.classify(item)
        except LLMProviderError as error:
            with self.repository.database.connect() as connection:
                self.repository._audit(
                    connection,
                    "classification_run",
                    transaction_id,
                    "llm_failed",
                    None,
                    {"error": type(error).__name__, "profile": profile_code},
                )
            raise
        if suggestion is None:
            return None
        return self.repository.classify(
            transaction_id,
            profile_code,
            suggestion.category,
            "llm",
            suggestion.confidence,
            suggestion.rationale,
            "suggested",
            transaction["version"],
            model_version=f"{suggestion.provider}/{suggestion.model}",
            prompt_hash=suggestion.prompt_hash,
            metadata={
                "prompt_version": suggestion.prompt_version,
                "taxonomy_version": suggestion.taxonomy_version,
                "redaction_mode": "standard-no-document-content",
                "duration_ms": suggestion.duration_ms,
            },
        )

    def classify_deterministic(self, transaction_id: str, profile_code: str) -> str | None:
        transaction = self._transaction(transaction_id)
        if not self.profile_available(transaction["account_id"], profile_code):
            return None
        for stage, finder in (
            ("rule", self.find_rule),
            ("history", self.find_history),
            ("hint", self.find_hint),
        ):
            if not self.stage_enabled_for_account(transaction["account_id"], stage):
                continue
            result = finder(transaction, profile_code)
            if result:
                return self.repository.classify(
                    transaction_id,
                    profile_code,
                    result.category_code,
                    result.stage,
                    result.confidence,
                    result.rationale,
                    expected_version=transaction["version"],
                    metadata={"source_id": result.source_id, **result.detail},
                )
        return None

    def find_rule(self, transaction: Mapping[str, Any], profile_code: str) -> StageResult | None:
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT r.*, c.code AS category_code FROM ledger_rules r
                   JOIN ledger_profiles p ON p.id=r.profile_id JOIN ledger_categories c ON c.id=r.category_id
                   WHERE p.code=? AND r.active=1 AND (r.account_id IS NULL OR r.account_id=?)
                   ORDER BY r.priority ASC, r.created_at ASC, r.id ASC""",
                (profile_code, transaction["account_id"]),
            ).fetchall()
        for rule in rows:
            if re.search(rule["pattern"], transaction["purpose"], re.IGNORECASE):
                return StageResult(
                    "rule",
                    rule["category_code"],
                    1.0,
                    f"Regel: {rule['name']}",
                    rule["id"],
                    {"pattern": rule["pattern"], "priority": rule["priority"]},
                )
        return None

    def find_history(self, transaction: Mapping[str, Any], profile_code: str) -> StageResult | None:
        fingerprint = self._fingerprint(transaction)
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT cl.id, t.purpose, t.counterparty, t.reference, c.code, cl.metadata_json
                   FROM ledger_classifications cl JOIN ledger_transactions t ON t.id=cl.transaction_id
                   JOIN ledger_categories c ON c.id=cl.category_id JOIN ledger_profiles p ON p.id=cl.profile_id
                   WHERE p.code=? AND cl.status='applied' AND cl.source IN ('manual', 'rule', 'hint', 'historical_transfer')
                     AND t.id<>? ORDER BY cl.created_at DESC""",
                (profile_code, transaction["id"]),
            ).fetchall()
        for row in rows:
            if self._fingerprint(row) == fingerprint:
                detail: dict[str, Any] = {"fingerprint": fingerprint}
                person_id = json.loads(row["metadata_json"] or "{}").get("person_id")
                if person_id:
                    detail["person_id"] = person_id
                return StageResult(
                    "history",
                    row["code"],
                    0.95,
                    "Confirmed historical merchant/reference classification",
                    row["id"],
                    detail,
                )
        return None

    def find_hint(self, transaction: Mapping[str, Any], profile_code: str) -> StageResult | None:
        searchable = " ".join(
            str(transaction.get(key) or "") for key in ("purpose", "counterparty", "reference")
        )
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT h.*, c.code AS category_code FROM ledger_hints h
                   JOIN ledger_profiles p ON p.id=h.profile_id JOIN ledger_categories c ON c.id=h.category_id
                   WHERE p.code=? AND h.active=1 AND h.pattern IS NOT NULL
                     AND (h.account_id IS NULL OR h.account_id=?)
                   ORDER BY h.priority, h.created_at, h.id""",
                (profile_code, transaction["account_id"]),
            ).fetchall()
        for hint in rows:
            if re.search(hint["pattern"], searchable, re.IGNORECASE):
                return StageResult(
                    "hint",
                    hint["category_code"],
                    0.9,
                    f"Nutzerhinweis: {hint['text']}",
                    hint["id"],
                    {"pattern": hint["pattern"], "priority": hint["priority"]},
                )
        return None

    def explain(self, transaction_id: str, profile_code: str) -> list[dict[str, Any]]:
        transaction = self._transaction(transaction_id)
        if not self.profile_available(transaction["account_id"], profile_code):
            profiles = self.pipeline.account_profiles(transaction["account_id"])
            return [
                {
                    "stage": stage,
                    "status": "profile_inactive",
                    "active_profiles": [profile["code"] for profile in profiles],
                }
                for stage in ("rule", "history", "hint", "llm")
            ]
        stages = self.pipeline.stages(transaction["account_id"])
        result: list[dict[str, Any]] = []
        decided = False
        for stage, finder in (
            ("rule", self.find_rule),
            ("history", self.find_history),
            ("hint", self.find_hint),
        ):
            if not stages[stage]:
                result.append({"stage": stage, "status": "disabled"})
            elif decided:
                result.append({"stage": stage, "status": "not_reached"})
            else:
                match = finder(transaction, profile_code)
                if match:
                    result.append({"status": "match", **asdict(match)})
                    decided = True
                else:
                    result.append({"stage": stage, "status": "no_match"})
        if not stages["llm"]:
            result.append({"stage": "llm", "status": "disabled"})
        elif decided:
            result.append({"stage": "llm", "status": "not_reached"})
        else:
            result.append(
                {
                    "stage": "llm",
                    "status": "would_suggest" if self.llm else "unavailable",
                    "model": self.llm_model,
                }
            )
        return result

    def test_rule_pattern(
        self,
        pattern: str,
        *,
        profile_code: str,
        account_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"Invalid regular expression: {error}") from error
        query = "SELECT id, booking_date, purpose, account_id FROM ledger_transactions"
        values: list[object] = []
        if account_id:
            query += " WHERE account_id=?"
            values.append(account_id)
        query += " ORDER BY booking_date DESC, id DESC LIMIT ?"
        values.append(limit)
        with self.repository.database.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, values)]
        matches = [row for row in rows if compiled.search(row["purpose"])]
        matches = matches[:20]
        return {"profile_code": profile_code, "examined": len(rows), "matches": matches}

    def build_llm_input(
        self, transaction_id: str, profile_code: str
    ) -> LLMClassificationInput | None:
        transaction = self._transaction(transaction_id)
        categories, taxonomy_version = self._taxonomy(profile_code)
        if not categories:
            return None
        return LLMClassificationInput(
            transaction_id=transaction_id,
            booking_date=transaction["booking_date"],
            amount=str(Decimal(from_minor_units(transaction["amount_minor"]))),
            currency=transaction["currency"],
            purpose=redact_text(transaction["purpose"], self.privacy) or "",
            counterparty=redact_text(transaction["counterparty"], self.privacy),
            reference=redact_text(transaction["reference"], self.privacy),
            profile=profile_code,
            allowed_categories=tuple(categories),
            hints=tuple(self._hints(profile_code, transaction["account_id"])),
            taxonomy_version=taxonomy_version,
        )

    def classify_open(
        self, profile_code: str, *, limit: int = 100, account_scope: AccountScope | None = None
    ) -> list[dict]:
        results = []
        for transaction in self.repository.list_transactions(
            limit=None, profile_code=profile_code, account_scope=account_scope
        ):
            if transaction.get("category_code"):
                continue
            with self.repository.database.connect() as connection:
                pending = connection.execute(
                    """SELECT 1 FROM ledger_classifications cl JOIN ledger_profiles p ON p.id=cl.profile_id
                    WHERE cl.transaction_id=? AND p.code=? AND cl.status='suggested' LIMIT 1""",
                    (transaction["id"], profile_code),
                ).fetchone()
            if pending:
                continue
            results.append(
                {
                    "transaction_id": transaction["id"],
                    "classification_id": self.classify(transaction["id"], profile_code),
                }
            )
            if len(results) >= limit:
                break
        return results

    def _transaction(self, transaction_id: str) -> dict:
        return self.repository.get_transaction(transaction_id)

    def stage_enabled(self, transaction_id: str, stage: str) -> bool:
        return self.stage_enabled_for_account(
            self._transaction(transaction_id)["account_id"], stage
        )

    def stage_enabled_for_account(self, account_id: str, stage: str) -> bool:
        return self.pipeline.stages(account_id)[stage]

    def profile_available(self, account_id: str, profile_code: str) -> bool:
        profiles = self.pipeline.account_profiles(account_id)
        return not profiles or any(profile["code"] == profile_code for profile in profiles)

    def _hints(self, profile_code: str, account_id: str) -> list[str]:
        with self.repository.database.connect() as connection:
            return [
                redact_text(row["text"], self.privacy) or ""
                for row in connection.execute(
                    """SELECT h.text FROM ledger_hints h JOIN ledger_profiles p ON p.id=h.profile_id
                   WHERE p.code=? AND h.active=1 AND (h.account_id IS NULL OR h.account_id=?)
                   ORDER BY h.priority, h.created_at, h.id""",
                    (profile_code, account_id),
                )
            ]

    def _taxonomy(self, profile_code: str) -> tuple[list[str], str]:
        with self.repository.database.connect() as connection:
            categories = [
                row["code"]
                for row in connection.execute(
                    """SELECT c.code FROM ledger_categories c JOIN ledger_profile_categories pc ON pc.category_id=c.id
                   JOIN ledger_profiles p ON p.id=pc.profile_id WHERE p.code=? ORDER BY c.code""",
                    (profile_code,),
                )
            ]
        return categories, hashlib.sha256("\n".join(categories).encode()).hexdigest()[:16]

    @classmethod
    def _fingerprint(cls, row: Mapping[str, Any]) -> str:
        counterparty, reference, purpose = (
            cls._normalise(row[key] or "") for key in ("counterparty", "reference", "purpose")
        )
        return "|".join((counterparty, purpose)) if (counterparty or purpose) else reference

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r"\W+", "", value).casefold()

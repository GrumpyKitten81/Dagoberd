"""Apply configurable column annotations after tabular imports."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from account_eval.config import ImportAnnotationConfig
from account_eval.ledger.models import ImportCategoryAnnotation, NormalizedTransaction
from account_eval.ledger.organization import OrganizationService
from account_eval.ledger.repository import LedgerRepository


@dataclass(frozen=True, slots=True)
class TransactionAnnotationPlan:
    external_id: str
    account_external_id: str
    warnings: tuple[str, ...] = ()
    categories: dict[str, str] = field(default_factory=dict)
    category_labels: dict[str, str] = field(default_factory=dict)
    dimensions: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnnotationApplyResult:
    applied: int
    skipped: int
    warnings: tuple[str, ...]


class ImportAnnotationService:
    def __init__(
        self,
        repository: LedgerRepository,
        organization: OrganizationService | None = None,
    ) -> None:
        self.repository = repository
        self.organization = organization or OrganizationService(repository)

    def plan(
        self,
        transactions: list[NormalizedTransaction],
        config: ImportAnnotationConfig,
    ) -> list[TransactionAnnotationPlan]:
        plans: list[TransactionAnnotationPlan] = []
        for transaction in transactions:
            raw = transaction.raw or {}
            warnings: list[str] = []
            categories: dict[str, str] = {}
            category_labels: dict[str, str] = {}
            dimensions: dict[str, str] = {}
            tags: list[str] = []

            if config.category_column and config.category_profile:
                label = _cell(raw, config.category_column)
                if label and not _ignored(label, config.ignore_values):
                    code = config.category_aliases.get(label)
                    if code:
                        categories[config.category_profile] = code
                        category_labels[config.category_profile] = label
                    elif config.preserve_unknown_categories:
                        categories[config.category_profile] = _preserved_category_code(
                            config.category_profile, label
                        )
                        category_labels[config.category_profile] = label
                    else:
                        warnings.append(
                            f"Unbekannte Kategorie {label!r} for {transaction.external_id[:8]}"
                        )

            for column, dimension_code in config.dimensions.items():
                label = _cell(raw, column)
                if not label or _ignored(label, config.ignore_values):
                    continue
                if _ignored(label, config.dimension_ignore_values):
                    continue
                value_code = config.dimension_aliases.get(label)
                if value_code:
                    dimensions[dimension_code] = value_code
                else:
                    warnings.append(
                        f"Unbekannter Dimensionswert {label!r} ({dimension_code}) "
                        f"for {transaction.external_id[:8]}"
                    )

            for column, profile_code in config.profile_columns.items():
                label = _cell(raw, column)
                if not label or _ignored(label, config.ignore_values):
                    continue
                aliases = config.profile_category_aliases.get(profile_code, {})
                code = aliases.get(label)
                if code:
                    categories[profile_code] = code
                    category_labels[profile_code] = label
                elif config.preserve_unknown_categories:
                    categories[profile_code] = _preserved_category_code(profile_code, label)
                    category_labels[profile_code] = label
                else:
                    warnings.append(
                        f"Unbekannte Profilkategorie {label!r} ({profile_code}) "
                        f"for {transaction.external_id[:8]}"
                    )

            for column, tag_name in config.tag_if_positive.items():
                raw_value = raw.get(column)
                if raw_value is None or str(raw_value).strip() == "":
                    continue
                try:
                    numeric = Decimal(str(raw_value).replace(",", "."))
                except Exception:
                    warnings.append(f"Invalid tag value in column {column!r}")
                    continue
                if numeric > 0:
                    tags.append(tag_name)

            plans.append(
                TransactionAnnotationPlan(
                    external_id=transaction.external_id,
                    account_external_id=transaction.account_external_id,
                    warnings=tuple(warnings),
                    categories=categories,
                    category_labels=category_labels,
                    dimensions=dimensions,
                    tags=tuple(tags),
                )
            )
        return plans

    def apply(
        self,
        account_id: str | None,
        plans: list[TransactionAnnotationPlan],
        *,
        provider_kind: str | None = None,
        provider_name: str | None = None,
        include_categories: bool = True,
    ) -> AnnotationApplyResult:
        applied = 0
        skipped = 0
        warnings: list[str] = []
        for plan in plans:
            warnings.extend(plan.warnings)
            transaction = self._transaction_for_external(
                account_id,
                plan.account_external_id,
                plan.external_id,
                provider_kind=provider_kind,
                provider_name=provider_name,
            )
            if transaction is None:
                skipped += 1
                continue
            transaction_id = transaction["id"]
            version = int(transaction["version"])
            changed = False
            if include_categories:
                for profile_code, category_code in plan.categories.items():
                    self.repository.classify(
                        transaction_id,
                        profile_code,
                        category_code,
                        source="manual",
                        confidence=1.0,
                        rationale="Import annotation",
                        status="applied",
                        expected_version=version,
                    )
                    version += 1
                    changed = True
            for dimension_code, value_code in plan.dimensions.items():
                self._ensure_dimension_value(dimension_code, value_code)
                result = self.organization.set_transaction_dimension(
                    transaction_id,
                    dimension_code,
                    value_code=value_code,
                    expected_version=version,
                )
                version = int(result["version"])
                changed = bool(result.get("changed", True)) or changed
            for tag_name in plan.tags:
                self.organization.create_tag(tag_name)
                result = self.organization.tag_transaction(
                    transaction_id,
                    tag_name,
                    expected_version=version,
                )
                version = int(result["version"])
                changed = bool(result.get("changed", True)) or changed
            if changed:
                applied += 1
            else:
                skipped += 1
        return AnnotationApplyResult(applied=applied, skipped=skipped, warnings=tuple(warnings))

    @staticmethod
    def category_annotations(
        plans: list[TransactionAnnotationPlan],
    ) -> tuple[ImportCategoryAnnotation, ...]:
        return tuple(
            ImportCategoryAnnotation(
                external_id=plan.external_id,
                account_external_id=plan.account_external_id,
                profile_code=profile_code,
                category_code=category_code,
                category_name=plan.category_labels.get(profile_code, category_code),
            )
            for plan in plans
            for profile_code, category_code in plan.categories.items()
        )

    def _transaction_for_external(
        self,
        account_id: str | None,
        account_external_id: str,
        external_id: str,
        *,
        provider_kind: str | None,
        provider_name: str | None,
    ) -> dict[str, Any] | None:
        filters = ["st.external_id=?"]
        parameters: list[object] = [external_id]
        if account_id:
            filters.append("t.account_id=?")
            parameters.append(account_id)
        else:
            filters.append("a.external_id=?")
            parameters.append(account_external_id)
            if provider_kind:
                filters.append("p.kind=?")
                parameters.append(provider_kind)
            if provider_name:
                filters.append("p.display_name=?")
                parameters.append(provider_name)
        with self.repository.database.connect() as connection:
            row = connection.execute(
                f"""SELECT t.id, t.version
                    FROM ledger_transactions t
                    JOIN ledger_transaction_sources link ON link.transaction_id=t.id
                    JOIN ledger_source_transactions st ON st.id=link.source_transaction_id
                    JOIN ledger_accounts a ON a.id=st.account_id
                    JOIN ledger_providers p ON p.id=st.provider_id
                    WHERE {" AND ".join(filters)}
                    ORDER BY link.is_primary DESC LIMIT 1""",
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def _ensure_dimension_value(self, dimension_code: str, value_code: str) -> None:
        dimensions = self.organization.list_dimensions()
        if not any(item["code"] == dimension_code for item in dimensions):
            self.organization.create_dimension(
                dimension_code,
                dimension_code.replace("-", " ").title(),
            )
        values: list[dict[str, Any]] = next(
            (item["values"] for item in dimensions if item["code"] == dimension_code),
            [],
        )
        if not any(value["code"] == value_code for value in values):
            self.organization.create_dimension_value(
                dimension_code,
                value_code,
                value_code.replace("-", " ").title(),
            )


def _cell(raw: dict[str, Any], column: str) -> str | None:
    value = raw.get(column)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _ignored(value: str, ignored: tuple[str, ...]) -> bool:
    return value in ignored or value.casefold() in {item.casefold() for item in ignored}


def slugify_label(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower().strip())
    return normalized.strip("-") or "value"


def _preserved_category_code(profile_code: str, label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    return f"{profile_code}.{slugify_label(label)[:48]}-{digest}"

"""Versioned, reproducible personal-finance analyses and renderers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dagoberd.accounts import AccountScope
from dagoberd.ledger.migrations import CURRENT_SCHEMA_VERSION
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError

ANALYSIS_CONTRACT_VERSION = "dagoberd-analysis-v1"
AnalysisSection = Literal[
    "cashflow",
    "income_expense",
    "fixed_costs",
    "trends",
    "net_worth",
]
AnalysisFormat = Literal["json", "xlsx", "html"]


class AnalysisDefinition(BaseModel):
    """Persisted contract for one repeatable personal analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_scope: AccountScope = Field(default_factory=AccountScope.all)
    profile: str | None = None
    booking_from: str | None = None
    booking_to: str | None = None
    relative_months: int | None = Field(default=None, ge=1, le=120)
    text: str | None = None
    amount_min_minor: int | None = None
    amount_max_minor: int | None = None
    currencies: tuple[str, ...] = ()
    booking_statuses: tuple[Literal["pending", "booked", "reversed", "corrected"], ...] = (
        "pending",
        "booked",
    )
    category_codes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    dimensions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    merchant: str | None = None
    document_status: Literal["matched", "unmatched"] | None = None
    base_currency: str | None = None
    fixed_cost_tag: str = "fixed"
    stale_balance_days: int = Field(default=31, ge=1, le=3650)
    sections: tuple[AnalysisSection, ...] = (
        "cashflow",
        "income_expense",
        "fixed_costs",
        "trends",
        "net_worth",
    )
    formats: tuple[AnalysisFormat, ...] = ("json", "xlsx", "html")
    group_by: tuple[str, ...] = ()

    @field_validator("booking_from", "booking_to")
    @classmethod
    def validate_dates(cls, value: str | None) -> str | None:
        if value:
            date.fromisoformat(value)
        return value

    @field_validator("base_currency")
    @classmethod
    def normalize_base_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3:
            raise ValueError("base_currency must be a three-letter ISO code")
        return normalized

    @field_validator("currencies")
    @classmethod
    def normalize_currencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip().upper() for value in values))
        if any(len(value) != 3 for value in normalized):
            raise ValueError("currencies must contain three-letter ISO codes")
        return normalized

    @model_validator(mode="after")
    def validate_period_and_sections(self) -> AnalysisDefinition:
        if self.relative_months and (self.booking_from or self.booking_to):
            raise ValueError("relative_months cannot be combined with fixed booking dates")
        if (
            self.booking_from
            and self.booking_to
            and date.fromisoformat(self.booking_from) > date.fromisoformat(self.booking_to)
        ):
            raise ValueError("booking_from must not be after booking_to")
        if not self.sections:
            raise ValueError("At least one analysis section is required")
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("Analysis sections must be unique")
        if not self.formats:
            raise ValueError("At least one output format is required")
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("Analysis formats must be unique")
        return self

    @field_validator("group_by")
    @classmethod
    def validate_group_by(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"category", "month", "account", "merchant", "tag"}
        for token in values:
            if token in allowed:
                continue
            if token.startswith("dimension:") and token.split(":", 1)[1].strip():
                continue
            raise ValueError(f"Unsupported group_by token: {token}")
        if len(set(values)) != len(values):
            raise ValueError("group_by tokens must be unique")
        return values


class AnalysisService:
    """Canonical analysis engine shared by CLI, MCP and all file formats."""

    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository

    @staticmethod
    def _clean(value: str, label: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError(f"{label} must not be empty")
        return cleaned

    @staticmethod
    def _code(value: str) -> str:
        code = value.strip().casefold().replace(" ", "-")
        if not code or not all(character.isalnum() or character in "-_." for character in code):
            raise ValueError("Analysis code may only contain letters, numbers, -, _ and .")
        return code

    def create_saved_analysis(
        self,
        code: str,
        name: str,
        definition: AnalysisDefinition | dict[str, Any],
        *,
        description: str = "",
    ) -> dict[str, Any]:
        normalized_code = self._code(code)
        normalized_name = self._clean(name, "Analysis name")
        validated = AnalysisDefinition.model_validate(definition)
        identifier = self.repository._id("analysis")
        with self.repository.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM ledger_saved_analyses WHERE code=? COLLATE NOCASE",
                (normalized_code,),
            ).fetchone():
                raise ValueError(f"Analysis code already exists: {normalized_code}")
            self.repository.resolve_account_scope(validated.account_scope)
            connection.execute(
                """INSERT INTO ledger_saved_analyses(
                       id, code, name, description, definition_json,
                       contract_version
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    normalized_code,
                    normalized_name,
                    description.strip(),
                    validated.model_dump_json(),
                    ANALYSIS_CONTRACT_VERSION,
                ),
            )
            self.repository._audit(
                connection,
                "saved_analysis",
                identifier,
                "create",
                None,
                {
                    "code": normalized_code,
                    "name": normalized_name,
                    "definition": validated.model_dump(mode="json"),
                    "contract_version": ANALYSIS_CONTRACT_VERSION,
                },
            )
        return self.get_saved_analysis(identifier)

    def update_saved_analysis(
        self,
        analysis_id_or_code: str,
        definition: AnalysisDefinition | dict[str, Any],
        *,
        expected_version: int,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        validated = AnalysisDefinition.model_validate(definition)
        self.repository.resolve_account_scope(validated.account_scope)
        with self.repository.database.connect() as connection:
            row = self._saved_row(connection, analysis_id_or_code)
            if row["version"] != expected_version:
                raise VersionConflictError(
                    "The saved analysis has changed; reload it before writing."
                )
            next_name = self._clean(name, "Analysis name") if name is not None else row["name"]
            next_description = (
                description.strip() if description is not None else row["description"]
            )
            connection.execute(
                """UPDATE ledger_saved_analyses
                   SET name=?, description=?, definition_json=?,
                       version=version+1, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    next_name,
                    next_description,
                    validated.model_dump_json(),
                    row["id"],
                ),
            )
            self.repository._audit(
                connection,
                "saved_analysis",
                row["id"],
                "update",
                {"version": row["version"], "definition": json.loads(row["definition_json"])},
                {"version": row["version"] + 1, "definition": validated.model_dump(mode="json")},
            )
        return self.get_saved_analysis(row["id"])

    @staticmethod
    def _saved_row(connection: sqlite3.Connection, analysis_id_or_code: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM ledger_saved_analyses
               WHERE id=? OR code=? COLLATE NOCASE""",
            (analysis_id_or_code, analysis_id_or_code),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown saved analysis {analysis_id_or_code}")
        return row

    def get_saved_analysis(self, analysis_id_or_code: str) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            row = self._saved_row(connection, analysis_id_or_code)
        result = dict(row)
        result["definition"] = json.loads(result.pop("definition_json"))
        return result

    def list_saved_analyses(self) -> list[dict[str, Any]]:
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM ledger_saved_analyses
                   ORDER BY code COLLATE NOCASE"""
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["definition"] = json.loads(item.pop("definition_json"))
            output.append(item)
        return output

    def add_exchange_rate(
        self,
        source_currency: str,
        target_currency: str,
        rate: Decimal,
        *,
        rate_date: date,
        source: str,
        source_version: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        source_code = source_currency.strip().upper()
        target_code = target_currency.strip().upper()
        if len(source_code) != 3 or len(target_code) != 3:
            raise ValueError("Currencies must be three-letter ISO codes")
        if source_code == target_code:
            raise ValueError("Exchange rate currencies must differ")
        if rate <= 0:
            raise ValueError("Exchange rate must be positive")
        source_name = self._clean(source, "Rate source")
        version = self._clean(source_version, "Rate source version")
        raw_key = idempotency_key.strip()
        if len(raw_key) < 8:
            raise ValueError("idempotency_key must contain at least 8 characters")
        key = hashlib.sha256(f"exchange-rate|{raw_key}".encode()).hexdigest()
        rate_text = format(rate, "f")
        expected = {
            "source_currency": source_code,
            "target_currency": target_code,
            "rate_decimal": rate_text,
            "rate_date": rate_date.isoformat(),
            "source": source_name,
            "source_version": version,
        }
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ledger_exchange_rates WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                if {field: existing[field] for field in expected} != expected:
                    raise ValueError("idempotency_key was already used with different rate data")
                return {**dict(existing), "created": False}
            duplicate = connection.execute(
                """SELECT * FROM ledger_exchange_rates
                   WHERE source_currency=? AND target_currency=?
                     AND rate_date=? AND source=? AND source_version=?""",
                (source_code, target_code, rate_date.isoformat(), source_name, version),
            ).fetchone()
            if duplicate:
                if duplicate["rate_decimal"] != rate_text:
                    raise ValueError("Rate source/version already exists with a different value")
                return {**dict(duplicate), "created": False}
            identifier = self.repository._id("exchange-rate")
            connection.execute(
                """INSERT INTO ledger_exchange_rates(
                       id, source_currency, target_currency, rate_decimal,
                       rate_date, source, source_version, idempotency_key
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    source_code,
                    target_code,
                    rate_text,
                    rate_date.isoformat(),
                    source_name,
                    version,
                    key,
                ),
            )
            self.repository._audit(
                connection,
                "exchange_rate",
                identifier,
                "create",
                None,
                expected,
                idempotency_key=f"exchange-rate:{key}",
            )
            return {
                **dict(
                    connection.execute(
                        "SELECT * FROM ledger_exchange_rates WHERE id=?", (identifier,)
                    ).fetchone()
                ),
                "created": True,
            }

    def list_exchange_rates(
        self,
        *,
        source_currency: str | None = None,
        target_currency: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        query = """SELECT id, source_currency, target_currency, rate_decimal,
                          rate_date, source, source_version, created_at
                   FROM ledger_exchange_rates WHERE 1=1"""
        parameters: list[Any] = []
        if source_currency:
            query += " AND source_currency=?"
            parameters.append(source_currency.upper())
        if target_currency:
            query += " AND target_currency=?"
            parameters.append(target_currency.upper())
        query += " ORDER BY rate_date DESC, created_at DESC LIMIT ?"
        parameters.append(limit)
        with self.repository.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def run_saved_analysis(
        self,
        analysis_id_or_code: str,
        output_dir: str | Path,
        *,
        as_of: date | None = None,
        formats: tuple[AnalysisFormat, ...] | None = None,
    ) -> dict[str, Any]:
        saved = self.get_saved_analysis(analysis_id_or_code)
        definition = AnalysisDefinition.model_validate(saved["definition"])
        selected_formats = formats or definition.formats
        AnalysisDefinition.model_validate(definition.model_dump() | {"formats": selected_formats})
        run_id = self.repository._id("analysis-run")
        result = self.analyze(
            definition,
            analysis_id=saved["id"],
            analysis_code=saved["code"],
            definition_version=int(saved["version"]),
            run_id=run_id,
            as_of=as_of,
        )
        paths = self._render(
            result,
            output_dir,
            prefix=saved["code"],
            formats=selected_formats,
        )
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_analysis_runs(
                       id, saved_analysis_id, definition_version,
                       definition_json, result_json, result_hash,
                       output_paths_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    saved["id"],
                    saved["version"],
                    definition.model_dump_json(),
                    serialized,
                    result_hash,
                    json.dumps([str(path) for path in paths]),
                ),
            )
            self.repository._audit(
                connection,
                "analysis_run",
                run_id,
                "generate",
                None,
                {
                    "analysis_id": saved["id"],
                    "analysis_code": saved["code"],
                    "definition_version": saved["version"],
                    "result_hash": result_hash,
                    "paths": [str(path) for path in paths],
                },
            )
        return {
            "run_id": run_id,
            "analysis_id": saved["id"],
            "analysis_code": saved["code"],
            "definition_version": saved["version"],
            "result_hash": result_hash,
            "paths": [str(path) for path in paths],
            "result": result,
        }

    def analyze(
        self,
        definition: AnalysisDefinition,
        *,
        analysis_id: str | None = None,
        analysis_code: str | None = None,
        definition_version: int = 1,
        run_id: str | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        execution_date = as_of or date.today()
        booking_from, booking_to = self._resolved_period(definition, execution_date)
        valuation_date = date.fromisoformat(booking_to) if booking_to else execution_date
        resolved_accounts = self.repository.resolve_account_scope(definition.account_scope)
        rows = self.repository.list_transactions(
            limit=None,
            booking_from=booking_from,
            booking_to=booking_to,
            profile_code=definition.profile,
            account_scope=definition.account_scope,
            booking_statuses=definition.booking_statuses,
        )
        rows = [row for row in rows if self._matches(row, definition)]
        rate_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        used_rates: dict[str, dict[str, Any]] = {}
        converted: list[dict[str, Any]] = []
        unconverted_currencies: set[str] = set()
        for row in rows:
            amount = int(row["cashflow_amount_minor"])
            conversion = self._convert_minor(
                amount,
                row["currency"],
                definition.base_currency,
                str(row["booking_date"]),
                rate_cache,
            )
            if conversion["rate"]:
                rate = conversion["rate"]
                used_rates[rate["snapshot_key"]] = rate
            if definition.base_currency and amount and conversion["amount_minor"] is None:
                unconverted_currencies.add(row["currency"])
            converted.append(
                {**row, "base_amount_minor": conversion["amount_minor"], "rate": conversion["rate"]}
            )

        warnings: list[str] = []
        if unconverted_currencies:
            warnings.append(
                "Incomplete base currency total; missing exchange rates for: "
                + ", ".join(sorted(unconverted_currencies))
            )
        cashflow = self._cashflow(converted, definition.base_currency)
        categories = self._group_transactions(
            converted,
            lambda row: row.get("category_code") or "unclassified",
            "category",
            definition.base_currency,
        )
        months = self._group_transactions(
            converted,
            lambda row: str(row["booking_date"])[:7],
            "month",
            definition.base_currency,
        )
        accounts = self._group_transactions(
            converted,
            lambda row: row["account_id"],
            "account_id",
            definition.base_currency,
            labels=lambda row: row.get("account_name") or row["account_id"],
        )
        fixed_rows = [
            row
            for row in converted
            if definition.fixed_cost_tag.casefold()
            in {str(tag).casefold() for tag in row.get("tags") or ()}
            or str(row.get("category_code") or "").startswith("expense.fixed")
        ]
        fixed_costs = {
            "definition": {
                "tag": definition.fixed_cost_tag,
                "category_prefix": "expense.fixed",
            },
            "by_currency": self._currency_totals(fixed_rows),
            "by_merchant": self._group_transactions(
                fixed_rows,
                lambda row: row.get("merchant") or row.get("counterparty") or "unknown",
                "merchant",
                definition.base_currency,
            ),
            "transaction_count": len(fixed_rows),
        }
        trends = self._trend_sections(months, definition.base_currency)
        net_worth = self._net_worth(
            resolved_accounts,
            valuation_date,
            definition.base_currency,
            definition.stale_balance_days,
            rate_cache,
            used_rates,
        )
        warnings.extend(net_worth.pop("warnings"))
        quality = {
            "pending_count": sum(row["booking_status"] == "pending" for row in converted),
            "unclassified_count": sum(not row.get("category_code") for row in converted),
            "unconverted_transaction_count": sum(
                bool(definition.base_currency)
                and int(row["cashflow_amount_minor"]) != 0
                and row["base_amount_minor"] is None
                for row in converted
            ),
            "missing_balance_account_count": sum(
                position["balance_minor"] is None for position in net_worth["positions"]
            ),
            "stale_balance_account_count": sum(
                bool(position["stale"]) for position in net_worth["positions"]
            ),
            "warnings": warnings,
        }
        fingerprint_payload = {
            "transactions": [
                (row["id"], row["version"], row["cashflow_amount_minor"])
                for row in sorted(converted, key=lambda item: item["id"])
            ],
            "balances": [
                (position["account_id"], position.get("snapshot_id"), position.get("balance_minor"))
                for position in net_worth["positions"]
            ],
            "rates": sorted(used_rates),
        }
        data_fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode()
        ).hexdigest()
        selected = set(definition.sections)
        result: dict[str, Any] = {
            "contract_version": ANALYSIS_CONTRACT_VERSION,
            "metadata": {
                "run_id": run_id,
                "analysis_id": analysis_id,
                "analysis_code": analysis_code,
                "definition_version": definition_version,
                "generated_at": datetime.now(UTC).isoformat(),
                "booking_from": booking_from,
                "booking_to": booking_to,
                "valuation_date": valuation_date.isoformat(),
                "account_scope": definition.account_scope.model_dump(mode="json"),
                "resolved_account_ids": list(resolved_accounts),
                "profile": definition.profile,
                "base_currency": definition.base_currency,
                "transaction_count": len(converted),
                "ledger_schema_version": CURRENT_SCHEMA_VERSION,
                "data_fingerprint": data_fingerprint,
                "exchange_rates": [used_rates[key] for key in sorted(used_rates)],
                "definition": definition.model_dump(mode="json"),
                "disclaimer": "Local analysis; not tax or investment advice.",
            },
            "quality": quality,
        }
        if "cashflow" in selected or "income_expense" in selected:
            result["cashflow"] = cashflow
            result["categories"] = categories
            result["months"] = months
            result["accounts"] = accounts
            if definition.group_by:
                result["groupings"] = {
                    token: self._group_by_token(converted, token, definition.base_currency)
                    for token in definition.group_by
                }
        if "fixed_costs" in selected:
            result["fixed_costs"] = fixed_costs
        if "trends" in selected:
            result["trends"] = trends
        if "net_worth" in selected:
            result["net_worth"] = net_worth
        return result

    @staticmethod
    def _resolved_period(
        definition: AnalysisDefinition, as_of: date
    ) -> tuple[str | None, str | None]:
        if definition.relative_months:
            month_index = as_of.year * 12 + as_of.month - definition.relative_months
            year, zero_based_month = divmod(month_index, 12)
            start = date(year, zero_based_month + 1, 1)
            return start.isoformat(), as_of.isoformat()
        return definition.booking_from, definition.booking_to

    @staticmethod
    def _matches(row: dict[str, Any], definition: AnalysisDefinition) -> bool:
        if definition.text:
            needle = definition.text.casefold()
            searchable = " ".join(
                str(row.get(field) or "")
                for field in (
                    "purpose",
                    "counterparty",
                    "reference",
                    "merchant",
                    "category_code",
                )
            ).casefold()
            searchable += " " + " ".join(row.get("tags") or ()).casefold()
            searchable += (
                " "
                + " ".join(
                    str(value) for value in (row.get("dimensions") or {}).values()
                ).casefold()
            )
            if needle not in searchable:
                return False
        amount = int(row["amount_minor"])
        if definition.amount_min_minor is not None and amount < definition.amount_min_minor:
            return False
        if definition.amount_max_minor is not None and amount > definition.amount_max_minor:
            return False
        if definition.currencies and row["currency"] not in definition.currencies:
            return False
        if definition.category_codes and row.get("category_code") not in definition.category_codes:
            return False
        row_tags = {str(tag).casefold() for tag in row.get("tags") or ()}
        if any(tag.casefold() not in row_tags for tag in definition.tags):
            return False
        row_dimensions = row.get("dimensions") or {}
        for code, accepted in definition.dimensions.items():
            if code not in row_dimensions:
                return False
            if accepted and row_dimensions[code] not in accepted:
                return False
        if (
            definition.merchant
            and definition.merchant.casefold() not in str(row.get("merchant") or "").casefold()
        ):
            return False
        return not (
            definition.document_status and row.get("document_status") != definition.document_status
        )

    def _convert_minor(
        self,
        amount_minor: int,
        source_currency: str,
        target_currency: str | None,
        on_date: str,
        cache: dict[tuple[str, str, str], dict[str, Any] | None],
    ) -> dict[str, Any]:
        if target_currency is None:
            return {"amount_minor": None, "rate": None}
        if source_currency == target_currency or amount_minor == 0:
            return {"amount_minor": amount_minor, "rate": None}
        key = (source_currency, target_currency, on_date)
        if key not in cache:
            cache[key] = self._find_rate(source_currency, target_currency, on_date)
        rate = cache[key]
        if rate is None:
            return {"amount_minor": None, "rate": None}
        converted = int(
            (Decimal(amount_minor) * Decimal(rate["rate_decimal"])).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        return {"amount_minor": converted, "rate": rate}

    def _find_rate(
        self, source_currency: str, target_currency: str, on_date: str
    ) -> dict[str, Any] | None:
        with self.repository.database.connect() as connection:
            direct = connection.execute(
                """SELECT * FROM ledger_exchange_rates
                   WHERE source_currency=? AND target_currency=?
                     AND rate_date<=?
                   ORDER BY rate_date DESC, created_at DESC LIMIT 1""",
                (source_currency, target_currency, on_date),
            ).fetchone()
            if direct:
                result = dict(direct)
                result.pop("idempotency_key", None)
                result["inverted"] = False
                result["snapshot_key"] = result["id"]
                return result
            inverse = connection.execute(
                """SELECT * FROM ledger_exchange_rates
                   WHERE source_currency=? AND target_currency=?
                     AND rate_date<=?
                   ORDER BY rate_date DESC, created_at DESC LIMIT 1""",
                (target_currency, source_currency, on_date),
            ).fetchone()
        if inverse is None:
            return None
        result = dict(inverse)
        result.pop("idempotency_key", None)
        result["rate_decimal"] = format(Decimal(1) / Decimal(result["rate_decimal"]), "f")
        result["source_currency"] = source_currency
        result["target_currency"] = target_currency
        result["inverted"] = True
        result["snapshot_key"] = f"{result['id']}:inverse"
        return result

    @staticmethod
    def _currency_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            groups[row["currency"]].append(int(row["cashflow_amount_minor"]))
        return [
            AnalysisService._totals(currency, values) for currency, values in sorted(groups.items())
        ]

    @staticmethod
    def _totals(currency: str, values: list[int]) -> dict[str, Any]:
        return {
            "currency": currency,
            "income_minor": sum(value for value in values if value > 0),
            "expense_minor": sum(value for value in values if value < 0),
            "net_minor": sum(values),
            "transaction_count": len(values),
        }

    def _cashflow(self, rows: list[dict[str, Any]], base_currency: str | None) -> dict[str, Any]:
        by_currency = self._currency_totals(rows)
        base = None
        if base_currency:
            missing = [
                row
                for row in rows
                if int(row["cashflow_amount_minor"]) != 0 and row["base_amount_minor"] is None
            ]
            converted_values = [
                int(row["base_amount_minor"])
                for row in rows
                if row["base_amount_minor"] is not None
            ]
            converted_totals = self._totals(base_currency, converted_values)
            base = {
                **converted_totals,
                "complete": not missing,
                "missing_transaction_count": len(missing),
            }
            if missing:
                base.update({"income_minor": None, "expense_minor": None, "net_minor": None})
        return {"by_currency": by_currency, "base_currency": base}

    def _group_transactions(
        self,
        rows: list[dict[str, Any]],
        key_function: Callable[[dict[str, Any]], object],
        key_name: str,
        base_currency: str | None,
        *,
        labels: Callable[[dict[str, Any]], object] | None = None,
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[(str(key_function(row)), row["currency"])].append(row)
        output: list[dict[str, Any]] = []
        for (key, currency), members in sorted(groups.items()):
            values = [int(member["cashflow_amount_minor"]) for member in members]
            item = {key_name: key, **self._totals(currency, values)}
            if labels:
                item["label"] = str(labels(members[0]))
            if base_currency:
                converted_values = [
                    member["base_amount_minor"]
                    for member in members
                    if member["base_amount_minor"] is not None
                ]
                complete = len(converted_values) == len(members)
                item["base_currency"] = base_currency
                item["base_income_minor"] = (
                    sum(int(value) for value in converted_values if int(value) > 0)
                    if complete
                    else None
                )
                item["base_expense_minor"] = (
                    sum(int(value) for value in converted_values if int(value) < 0)
                    if complete
                    else None
                )
                item["base_net_minor"] = (
                    sum(int(value) for value in converted_values) if complete else None
                )
                item["base_complete"] = complete
            output.append(item)
        return output

    def _group_by_token(
        self,
        rows: list[dict[str, Any]],
        token: str,
        base_currency: str | None,
    ) -> list[dict[str, Any]]:
        if token == "category":
            return self._group_transactions(
                rows,
                lambda row: row.get("category_code") or "unclassified",
                "category",
                base_currency,
            )
        if token == "month":
            return self._group_transactions(
                rows,
                lambda row: str(row["booking_date"])[:7],
                "month",
                base_currency,
            )
        if token == "account":
            return self._group_transactions(
                rows,
                lambda row: row["account_id"],
                "account_id",
                base_currency,
                labels=lambda row: row.get("account_name") or row["account_id"],
            )
        if token == "merchant":
            return self._group_transactions(
                rows,
                lambda row: row.get("merchant") or row.get("counterparty") or "unknown",
                "merchant",
                base_currency,
            )
        if token == "tag":
            expanded: list[dict[str, Any]] = []
            for row in rows:
                tags = row.get("tags") or ()
                if not tags:
                    expanded.append({**row, "_group_tag": "untagged"})
                else:
                    for tag in tags:
                        expanded.append({**row, "_group_tag": tag})
            return self._group_transactions(
                expanded,
                lambda row: row["_group_tag"],
                "tag",
                base_currency,
            )
        if token.startswith("dimension:"):
            dimension_code = token.split(":", 1)[1]
            return self._group_transactions(
                rows,
                lambda row: (row.get("dimensions") or {}).get(dimension_code, "unset"),
                f"dimension_{dimension_code}",
                base_currency,
            )
        raise ValueError(f"Unsupported group_by token: {token}")

    @staticmethod
    def _trends(months: list[dict[str, Any]]) -> list[dict[str, Any]]:
        previous: dict[str, int] = {}
        trends = []
        for item in sorted(months, key=lambda row: (row["currency"], row["month"])):
            currency = item["currency"]
            current = int(item["net_minor"])
            trend = dict(item)
            trend["previous_net_minor"] = previous.get(currency)
            trend["net_change_minor"] = (
                current - previous[currency] if currency in previous else None
            )
            previous[currency] = current
            trends.append(trend)
        return sorted(trends, key=lambda row: (row["month"], row["currency"]))

    @classmethod
    def _trend_sections(
        cls,
        months: list[dict[str, Any]],
        base_currency: str | None,
    ) -> dict[str, Any]:
        by_currency = cls._trends(months)
        base_rows: list[dict[str, Any]] = []
        if base_currency:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in months:
                grouped[row["month"]].append(row)
            previous: int | None = None
            for month, members in sorted(grouped.items()):
                complete = all(bool(member.get("base_complete")) for member in members)
                income = (
                    sum(int(member["base_income_minor"]) for member in members)
                    if complete
                    else None
                )
                expense = (
                    sum(int(member["base_expense_minor"]) for member in members)
                    if complete
                    else None
                )
                net = sum(int(member["base_net_minor"]) for member in members) if complete else None
                base_rows.append(
                    {
                        "month": month,
                        "currency": base_currency,
                        "income_minor": income,
                        "expense_minor": expense,
                        "net_minor": net,
                        "complete": complete,
                        "previous_net_minor": previous if complete else None,
                        "net_change_minor": (
                            net - previous
                            if complete and net is not None and previous is not None
                            else None
                        ),
                    }
                )
                if complete and net is not None:
                    previous = net
        return {"by_currency": by_currency, "base_currency": base_rows}

    def _net_worth(
        self,
        account_ids: tuple[str, ...],
        as_of: date,
        base_currency: str | None,
        stale_balance_days: int,
        rate_cache: dict[tuple[str, str, str], dict[str, Any] | None],
        used_rates: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if not account_ids:
            return {
                "as_of": as_of.isoformat(),
                "positions": [],
                "by_currency": [],
                "base_currency": None,
                "warnings": [],
            }
        placeholders = ",".join("?" for _ in account_ids)
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                f"""SELECT a.id AS account_id, a.display_name AS account_name,
                            a.account_type, a.currency,
                            snapshot.id AS snapshot_id,
                            snapshot.balance_date, snapshot.balance_minor,
                            snapshot.source AS balance_source
                     FROM ledger_accounts a
                     LEFT JOIN ledger_balance_snapshots snapshot ON snapshot.id=(
                         SELECT latest.id FROM ledger_balance_snapshots latest
                         WHERE latest.account_id=a.id AND latest.balance_date<=?
                         ORDER BY latest.balance_date DESC, latest.created_at DESC
                         LIMIT 1
                     )
                     WHERE a.id IN ({placeholders}) ORDER BY a.display_name, a.id""",
                (as_of.isoformat(), *account_ids),
            ).fetchall()
        positions: list[dict[str, Any]] = []
        warnings: list[str] = []
        liability_types = {"credit_card", "loan", "liability"}
        for row in rows:
            balance = row["balance_minor"]
            contribution = None
            stale = False
            age_days = None
            if balance is None:
                warnings.append(f"No balance for account {row['account_name']}")
            else:
                contribution = (
                    -abs(int(balance)) if row["account_type"] in liability_types else int(balance)
                )
                age_days = (as_of - date.fromisoformat(row["balance_date"])).days
                stale = age_days > stale_balance_days
                if stale:
                    warnings.append(
                        f"Balance for account {row['account_name']} ist {age_days} Tage alt"
                    )
            conversion = (
                self._convert_minor(
                    contribution or 0,
                    row["currency"],
                    base_currency,
                    row["balance_date"] or as_of.isoformat(),
                    rate_cache,
                )
                if contribution is not None
                else {"amount_minor": None, "rate": None}
            )
            if conversion["rate"]:
                rate = conversion["rate"]
                used_rates[rate["snapshot_key"]] = rate
            positions.append(
                {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "account_type": row["account_type"],
                    "currency": row["currency"],
                    "snapshot_id": row["snapshot_id"],
                    "balance_date": row["balance_date"],
                    "balance_source": row["balance_source"],
                    "balance_minor": int(balance) if balance is not None else None,
                    "contribution_minor": contribution,
                    "base_currency": base_currency,
                    "base_contribution_minor": conversion["amount_minor"],
                    "age_days": age_days,
                    "stale": stale,
                }
            )
        by_currency_values: dict[str, list[int]] = defaultdict(list)
        for position in positions:
            if position["contribution_minor"] is not None:
                by_currency_values[position["currency"]].append(position["contribution_minor"])
        by_currency = [
            {"currency": currency, "net_worth_minor": sum(values), "position_count": len(values)}
            for currency, values in sorted(by_currency_values.items())
        ]
        base = None
        if base_currency:
            valued = [
                position for position in positions if position["contribution_minor"] is not None
            ]
            complete = all(position["base_contribution_minor"] is not None for position in valued)
            base = {
                "currency": base_currency,
                "complete": complete,
                "net_worth_minor": (
                    sum(int(position["base_contribution_minor"]) for position in valued)
                    if complete
                    else None
                ),
                "missing_position_count": sum(
                    position["contribution_minor"] is not None
                    and position["base_contribution_minor"] is None
                    for position in positions
                ),
            }
            if not complete:
                warnings.append("Incomplete net worth total in base currency")
        return {
            "as_of": as_of.isoformat(),
            "positions": positions,
            "by_currency": by_currency,
            "base_currency": base,
            "warnings": warnings,
        }

    def net_worth_snapshot(
        self,
        account_ids: tuple[str, ...],
        *,
        as_of: date,
        base_currency: str | None,
        stale_balance_days: int = 31,
    ) -> dict[str, Any]:
        """Return the canonical read-only net-worth view for one date."""
        return self._net_worth(
            account_ids,
            as_of,
            base_currency,
            stale_balance_days,
            {},
            {},
        )

    def _render(
        self,
        result: dict[str, Any],
        output_dir: str | Path,
        *,
        prefix: str,
        formats: tuple[AnalysisFormat, ...],
    ) -> list[Path]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        base = target / f"{prefix}-{stamp}-analysis"
        tables = self._tables(result)
        paths: list[Path] = []
        if "json" in formats:
            path = base.with_suffix(".json")
            path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths.append(path)
        if "xlsx" in formats:
            path = base.with_suffix(".xlsx")
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                for name, frame in tables.items():
                    frame.to_excel(writer, sheet_name=name[:31], index=False)
            paths.append(path)
        if "html" in formats:
            path = base.with_suffix(".html")
            embedded = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
            parts = [
                "<html><body><h1>Dagoberd Analysis</h1>",
                '<script id="dagoberd-analysis" type="application/json">',
                embedded,
                "</script>",
            ]
            for name, frame in tables.items():
                parts.append(f"<h2>{name}</h2>")
                parts.append(frame.to_html(index=False, escape=True))
            parts.append("</body></html>")
            path.write_text("".join(parts), encoding="utf-8")
            paths.append(path)
        return paths

    @staticmethod
    def _tables(result: dict[str, Any]) -> dict[str, pd.DataFrame]:
        metadata = dict(result["metadata"])
        for key, value in list(metadata.items()):
            if isinstance(value, (dict, list, tuple)):
                metadata[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        tables: dict[str, pd.DataFrame] = {
            "Metadata": pd.DataFrame([metadata]),
            "Quality": pd.DataFrame(
                [
                    {
                        key: json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (list, dict))
                        else value
                        for key, value in result["quality"].items()
                    }
                ]
            ),
        }
        if "cashflow" in result:
            tables["Cashflow"] = pd.DataFrame(result["cashflow"]["by_currency"])
            tables["Cashflow Base"] = pd.DataFrame(
                [result["cashflow"]["base_currency"]] if result["cashflow"]["base_currency"] else []
            )
            tables["Categories"] = pd.DataFrame(result["categories"])
            tables["Months"] = pd.DataFrame(result["months"])
            tables["Accounts"] = pd.DataFrame(result["accounts"])
        if "fixed_costs" in result:
            tables["Fixed Costs"] = pd.DataFrame(result["fixed_costs"]["by_currency"])
            tables["Fixed Merchants"] = pd.DataFrame(result["fixed_costs"]["by_merchant"])
        if "trends" in result:
            tables["Trends"] = pd.DataFrame(result["trends"]["by_currency"])
            tables["Trends Base"] = pd.DataFrame(result["trends"]["base_currency"])
        if "net_worth" in result:
            tables["Net Worth"] = pd.DataFrame(result["net_worth"]["positions"])
            tables["Net Worth Currency"] = pd.DataFrame(result["net_worth"]["by_currency"])
            tables["Net Worth Base"] = pd.DataFrame(
                [result["net_worth"]["base_currency"]]
                if result["net_worth"]["base_currency"]
                else []
            )
        return tables

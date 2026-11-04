"""Read-only projections for the local GUI overview."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Literal

from dagoberd.accounts import AccountScope
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.reporting.analysis import AnalysisDefinition, AnalysisService

DashboardPeriod = Literal["3m", "ytd", "12m", "all"]
LIABILITY_TYPES = {"credit_card", "loan", "liability"}


class DashboardService:
    """Build one consistent dashboard without persisting an analysis run."""

    def __init__(self, repository: LedgerRepository, base_currency: str | None) -> None:
        self.repository = repository
        self.base_currency = base_currency
        self.analysis = AnalysisService(repository)

    def view(
        self,
        *,
        account_ids: tuple[str, ...] = (),
        period: DashboardPeriod = "12m",
        as_of: date | None = None,
    ) -> dict[str, Any]:
        valuation_date = as_of or date.today()
        scope = AccountScope.selected(account_ids) if account_ids else AccountScope.all()
        resolved_ids = self.repository.resolve_account_scope(scope)
        accounts = {
            account["id"]: account
            for account in self.repository.list_accounts(enabled_only=True)
            if account["id"] in resolved_ids
        }
        display_currency = self._display_currency(tuple(accounts.values()))
        booking_from = self._period_start(period, valuation_date, resolved_ids)
        definition = AnalysisDefinition(
            account_scope=scope,
            booking_from=booking_from.isoformat(),
            booking_to=valuation_date.isoformat(),
            base_currency=display_currency,
            sections=("cashflow", "trends", "net_worth"),
            formats=("json",),
        )
        analysis_result = self.analysis.analyze(definition, as_of=valuation_date)
        current = self.analysis.net_worth_snapshot(
            resolved_ids,
            as_of=valuation_date,
            base_currency=display_currency,
        )
        beginning = self.analysis.net_worth_snapshot(
            resolved_ids,
            as_of=booking_from,
            base_currency=display_currency,
        )
        positions = self._positions(current, beginning, accounts)
        quality = self._quality(
            current,
            analysis_result["quality"],
            positions,
            has_display_currency=bool(display_currency),
        )
        complete = (
            bool(display_currency)
            and not quality["missing_balance_count"]
            and not quality["missing_conversion_count"]
        )
        summary = self._summary(
            positions,
            analysis_result["trends"],
            display_currency,
            valuation_date,
            complete,
        )
        return {
            "scope": {
                "account_ids": list(resolved_ids),
                "period": period,
                "booking_from": booking_from.isoformat(),
                "booking_to": valuation_date.isoformat(),
                "as_of": valuation_date.isoformat(),
            },
            "summary": summary,
            "accounts": positions,
            "net_worth_series": self._net_worth_series(
                resolved_ids, booking_from, valuation_date, display_currency
            ),
            "cashflow_series": self._cashflow_series(
                analysis_result["trends"], booking_from, valuation_date, display_currency
            ),
            "composition": self._composition(positions, display_currency),
            "quality": quality,
        }

    def _display_currency(self, accounts: tuple[dict[str, Any], ...]) -> str | None:
        if self.base_currency:
            return self.base_currency
        currencies = {str(account["currency"]) for account in accounts}
        return next(iter(currencies)) if len(currencies) == 1 else None

    def _period_start(
        self, period: DashboardPeriod, as_of: date, account_ids: tuple[str, ...]
    ) -> date:
        if period == "ytd":
            return date(as_of.year, 1, 1)
        if period in {"3m", "12m"}:
            months = 3 if period == "3m" else 12
            return _shift_month(date(as_of.year, as_of.month, 1), -(months - 1))
        if period != "all":
            raise ValueError("Unsupported dashboard period")
        if not account_ids:
            return as_of
        placeholders = ",".join("?" for _ in account_ids)
        with self.repository.database.connect() as connection:
            row = connection.execute(
                f"""SELECT MIN(value) AS first_date FROM (
                         SELECT MIN(booking_date) AS value
                         FROM ledger_transactions
                         WHERE state<>'void' AND account_id IN ({placeholders})
                         UNION ALL
                         SELECT MIN(balance_date) AS value
                         FROM ledger_balance_snapshots
                         WHERE account_id IN ({placeholders})
                     )""",
                (*account_ids, *account_ids),
            ).fetchone()
        return date.fromisoformat(row["first_date"]) if row and row["first_date"] else as_of

    @staticmethod
    def _positions(
        current: dict[str, Any],
        beginning: dict[str, Any],
        accounts: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        starting = {item["account_id"]: item for item in beginning["positions"]}
        output = []
        for position in current["positions"]:
            account = accounts[position["account_id"]]
            prior = starting.get(position["account_id"], {})
            current_value = position.get("base_contribution_minor")
            prior_value = prior.get("base_contribution_minor")
            if current_value is None or prior_value is None:
                change = None
            else:
                change = int(current_value) - int(prior_value)
            section = (
                "missing"
                if position["balance_minor"] is None
                else "liabilities"
                if position["account_type"] in LIABILITY_TYPES
                else "assets"
            )
            output.append(
                {
                    **position,
                    "provider_kind": account["provider_kind"],
                    "provider_name": account["provider_name"],
                    "section": section,
                    "period_change_minor": change,
                }
            )
        return sorted(output, key=lambda item: (item["section"], item["account_name"].casefold()))

    def _quality(
        self,
        current: dict[str, Any],
        analysis_quality: dict[str, Any],
        positions: list[dict[str, Any]],
        *,
        has_display_currency: bool,
    ) -> dict[str, Any]:
        missing_balance = sum(item["balance_minor"] is None for item in positions)
        missing_conversion = sum(
            item["contribution_minor"] is not None
            and item["base_currency"] is not None
            and item["base_contribution_minor"] is None
            for item in positions
        )
        open_reviews = self._open_review_count(tuple(item["account_id"] for item in positions))
        warnings = list(
            dict.fromkeys([*current.get("warnings", ()), *analysis_quality["warnings"]])
        )
        return {
            "complete": has_display_currency and not missing_balance and not missing_conversion,
            "missing_display_currency": not has_display_currency,
            "missing_balance_count": missing_balance,
            "stale_balance_count": sum(bool(item["stale"]) for item in positions),
            "missing_conversion_count": missing_conversion,
            "pending_transaction_count": analysis_quality["pending_count"],
            "unclassified_transaction_count": analysis_quality["unclassified_count"],
            "open_review_count": open_reviews,
            "open_review_scope": "selected_accounts_and_global",
            "warnings": warnings,
        }

    def _open_review_count(self, account_ids: tuple[str, ...]) -> int:
        conditions = "ri.entity_type IN ('document', 'import_preview', 'system')"
        parameters: list[Any] = []
        if account_ids:
            placeholders = ",".join("?" for _ in account_ids)
            conditions = f"""{conditions} OR
                (ri.entity_type='transaction' AND EXISTS (
                    SELECT 1 FROM ledger_transactions t
                    WHERE t.id=ri.entity_id AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='classification' AND EXISTS (
                    SELECT 1 FROM ledger_classifications c
                    JOIN ledger_transactions t ON t.id=c.transaction_id
                    WHERE c.id=ri.entity_id AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='transaction_relation' AND EXISTS (
                    SELECT 1 FROM ledger_transaction_relations relation
                    JOIN ledger_transactions source
                      ON source.id=relation.source_transaction_id
                    JOIN ledger_transactions target
                      ON target.id=relation.target_transaction_id
                    WHERE relation.id=ri.entity_id
                      AND (source.account_id IN ({placeholders})
                           OR target.account_id IN ({placeholders}))
                )) OR
                (ri.entity_type='document_match' AND EXISTS (
                    SELECT 1 FROM ledger_document_matches match
                    JOIN ledger_transactions t ON t.id=match.transaction_id
                    WHERE match.id=ri.entity_id AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='reconciliation' AND EXISTS (
                    SELECT 1 FROM ledger_reconciliations reconciliation
                    WHERE reconciliation.id=ri.entity_id
                      AND reconciliation.account_id IN ({placeholders})
                ))"""
            parameters.extend(account_ids * 6)
        with self.repository.database.connect() as connection:
            return int(
                connection.execute(
                    f"""SELECT COUNT(*) FROM ledger_review_items ri
                        WHERE ri.status='open' AND ({conditions})""",
                    parameters,
                ).fetchone()[0]
            )

    @staticmethod
    def _summary(
        positions: list[dict[str, Any]],
        trends: dict[str, Any],
        currency: str | None,
        as_of: date,
        complete: bool,
    ) -> dict[str, Any]:
        contributions = {
            "assets": [
                int(item["base_contribution_minor"])
                for item in positions
                if item["section"] == "assets" and item["base_contribution_minor"] is not None
            ],
            "liabilities": [
                int(item["base_contribution_minor"])
                for item in positions
                if item["section"] == "liabilities" and item["base_contribution_minor"] is not None
            ],
        }
        current_month = as_of.strftime("%Y-%m")
        cashflow = next(
            (item for item in trends.get("base_currency") or () if item["month"] == current_month),
            None,
        )
        cashflow_complete = bool(currency) and (not cashflow or bool(cashflow["complete"]))
        month = {
            "income_minor": int(cashflow["income_minor"])
            if cashflow_complete and cashflow
            else 0
            if cashflow_complete
            else None,
            "expense_minor": int(cashflow["expense_minor"])
            if cashflow_complete and cashflow
            else 0
            if cashflow_complete
            else None,
            "net_minor": int(cashflow["net_minor"])
            if cashflow_complete and cashflow
            else 0
            if cashflow_complete
            else None,
            "complete": cashflow_complete,
        }
        return {
            "currency": currency,
            "complete": complete,
            "assets_minor": sum(contributions["assets"]) if complete else None,
            "liabilities_minor": -sum(contributions["liabilities"]) if complete else None,
            "net_worth_minor": sum((*contributions["assets"], *contributions["liabilities"]))
            if complete
            else None,
            "current_month": month,
        }

    def _net_worth_series(
        self,
        account_ids: tuple[str, ...],
        start: date,
        end: date,
        currency: str | None,
    ) -> list[dict[str, Any]]:
        output = []
        for point in _monthly_points(start, end):
            snapshot = self.analysis.net_worth_snapshot(
                account_ids,
                as_of=point,
                base_currency=currency,
            )
            missing = sum(item["balance_minor"] is None for item in snapshot["positions"])
            converted = snapshot.get("base_currency")
            complete = bool(converted and converted["complete"] and not missing)
            output.append(
                {
                    "date": point.isoformat(),
                    "currency": currency,
                    "net_worth_minor": int(converted["net_worth_minor"])
                    if converted and complete
                    else None,
                    "complete": complete,
                    "missing_balance_count": missing,
                    "stale_balance_count": sum(
                        bool(item["stale"]) for item in snapshot["positions"]
                    ),
                    "by_currency": snapshot["by_currency"],
                }
            )
        return output

    @staticmethod
    def _cashflow_series(
        trends: dict[str, Any], start: date, end: date, currency: str | None
    ) -> list[dict[str, Any]]:
        if currency:
            by_month = {item["month"]: item for item in trends.get("base_currency") or ()}
            output = []
            for point in _monthly_points(start, end):
                month = point.strftime("%Y-%m")
                item = by_month.get(month)
                output.append(
                    {
                        "month": month,
                        "currency": currency,
                        "income_minor": int(item["income_minor"])
                        if item and item["income_minor"] is not None
                        else 0
                        if not item
                        else None,
                        "expense_minor": int(item["expense_minor"])
                        if item and item["expense_minor"] is not None
                        else 0
                        if not item
                        else None,
                        "net_minor": int(item["net_minor"])
                        if item and item["net_minor"] is not None
                        else 0
                        if not item
                        else None,
                        "complete": not item or bool(item["complete"]),
                    }
                )
            return output
        return list(trends.get("by_currency") or ())

    @staticmethod
    def _composition(positions: list[dict[str, Any]], currency: str | None) -> list[dict[str, Any]]:
        return [
            {
                "account_id": item["account_id"],
                "label": item["account_name"],
                "section": item["section"],
                "currency": currency or item["currency"],
                "amount_minor": abs(
                    int(item["base_contribution_minor"] if currency else item["contribution_minor"])
                ),
            }
            for item in positions
            if item["section"] != "missing"
            and (item["base_contribution_minor"] if currency else item["contribution_minor"])
            is not None
        ]


def _shift_month(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _monthly_points(start: date, end: date) -> list[date]:
    points = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, last_day)
        points.append(min(month_end, end))
        cursor = _shift_month(cursor, 1)
    if points and points[-1] != end:
        points.append(end)
    return list(dict.fromkeys(points)) or [end]

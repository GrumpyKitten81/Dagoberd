"""Provider-neutral reports for the new ledger."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from dagoberd.accounts import AccountScope
from dagoberd.config import ReportExtensionConfig
from dagoberd.date_display import DisplayConfig, resolve_display
from dagoberd.ledger.migrations import CURRENT_SCHEMA_VERSION
from dagoberd.ledger.models import from_minor_units
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.reporting.display import write_excel_table
from dagoberd.reporting.extensions import (
    REPORT_EXTENSION_API_VERSION,
    ReportContext,
    load_report_extension,
    validate_report_artifacts,
)
from dagoberd.reporting.html_report import profile_report_title, render_profile_report_html


class LedgerReportGenerator:
    def __init__(
        self,
        repository: LedgerRepository,
        output_dir: str | Path,
        *,
        extensions: dict[str, ReportExtensionConfig] | None = None,
        display: DisplayConfig | None = None,
    ) -> None:
        self.repository = repository
        self.display = display or DisplayConfig()
        self.extensions = extensions or {}
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def dataframe(
        self,
        profile_code: str | None = None,
        *,
        account_scope: AccountScope | None = None,
        booking_from: str | None = None,
        booking_to: str | None = None,
        currency: str | None = None,
    ) -> pd.DataFrame:
        scope = account_scope or AccountScope.all()
        data = self.repository.list_transactions(
            limit=None,
            booking_from=booking_from,
            booking_to=booking_to,
            profile_code=profile_code,
            account_scope=scope,
        )
        frame = pd.DataFrame(data)
        if frame.empty:
            return frame
        if currency:
            frame = frame.loc[frame["currency"] == currency.upper()].copy()
        frame["amount"] = frame["amount_minor"].map(from_minor_units)
        frame["cashflow_amount"] = frame["cashflow_amount_minor"].map(from_minor_units)
        frame["booking_date"] = pd.to_datetime(frame["booking_date"])
        frame["valuta_date"] = pd.to_datetime(frame["valuta_date"], errors="coerce")
        return frame

    def generate(
        self,
        profile_code: str | None = None,
        formats: tuple[str, ...] = ("json", "xlsx", "html"),
        *,
        account_scope: AccountScope | None = None,
        booking_from: str | None = None,
        booking_to: str | None = None,
        currency: str | None = None,
        extension: str | None = None,
    ) -> list[Path]:
        if (
            not formats
            or len(set(formats)) != len(formats)
            or set(formats) - {"json", "xlsx", "html"}
        ):
            raise ValueError("Choose distinct report formats from json, xlsx and html")
        if extension is not None:
            if extension not in self.extensions:
                raise ValueError(f"Unknown report extension: {extension}")
            configured_profile = self.extensions[extension].profile
            if profile_code is not None and profile_code != configured_profile:
                raise ValueError("Report profile does not match the configured extension profile")
            profile_code = configured_profile
        scope = account_scope or AccountScope.all()
        frame = self.dataframe(
            profile_code,
            account_scope=scope,
            booking_from=booking_from,
            booking_to=booking_to,
            currency=currency,
        )
        metadata = self._metadata(
            profile_code,
            scope,
            booking_from,
            booking_to,
            len(frame),
            currency,
            frame,
            self.repository.transaction_status_counts(
                booking_from=booking_from,
                booking_to=booking_to,
                account_scope=scope,
                currency=currency,
            ),
        )
        prefix = profile_code or "general"
        if any(
            (self.output_dir / f"{prefix}-report.{suffix}").exists()
            for suffix in ("json", "xlsx", "html")
        ):
            prefix = f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        if extension is not None:
            extension_paths = self._generate_extension(extension, frame, metadata, formats)
            self._record_run(
                profile_code,
                scope,
                booking_from,
                booking_to,
                formats,
                extension_paths,
                len(frame),
                extension=extension,
            )
            return extension_paths
        summary = self._summary(frame)
        monthly = self._monthly_summary(frame)
        accounts = self._account_summary(frame)
        open_items = self._open_items(frame)
        presentation = resolve_display(self.display)
        paths: list[Path] = []
        if "json" in formats:
            path = self.output_dir / f"{prefix}-report.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata": metadata,
                        "transactions": frame.to_dict("records"),
                        "categories": summary.to_dict("records"),
                        "months": monthly.to_dict("records"),
                        "accounts": accounts.to_dict("records"),
                        "open_items": open_items.to_dict("records"),
                    },
                    default=str,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            paths.append(path)
        if "xlsx" in formats:
            path = self.output_dir / f"{prefix}-report.xlsx"
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                for sheet, table in {
                    "Transactions": frame,
                    "Cashflow categories": _display_table(summary),
                    "Months": _display_table(monthly),
                    "Accounts": _display_table(accounts),
                    "Open items": open_items,
                    "Metadata": pd.DataFrame([metadata]),
                }.items():
                    write_excel_table(table, writer, sheet, presentation)
            paths.append(path)
        if "html" in formats:
            path = self.output_dir / f"{prefix}-report.html"
            path.write_text(
                render_profile_report_html(
                    display=presentation,
                    title=profile_report_title(profile_code),
                    metadata=metadata,
                    categories=_display_table(summary),
                    months=_display_table(monthly),
                    accounts=_display_table(accounts),
                    open_items=open_items,
                    transactions=frame,
                ),
                encoding="utf-8",
            )
            paths.append(path)
        self._record_run(
            profile_code,
            scope,
            booking_from,
            booking_to,
            formats,
            paths,
            len(frame),
        )
        return paths

    def _record_run(
        self,
        profile_code: str | None,
        account_scope: AccountScope,
        booking_from: str | None,
        booking_to: str | None,
        formats: tuple[str, ...],
        paths: list[Path],
        count: int,
        *,
        extension: str | None = None,
    ) -> None:
        identifier = f"report-{uuid4()}"
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_report_runs(
                       id, profile_code, account_scope_json, booking_from,
                       booking_to, formats_json, output_paths_json,
                       transaction_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    profile_code,
                    account_scope.model_dump_json(),
                    booking_from,
                    booking_to,
                    json.dumps(formats),
                    json.dumps([str(path) for path in paths]),
                    count,
                ),
            )
            self.repository._audit(
                connection,
                "report",
                identifier,
                "generate",
                None,
                {
                    "profile": profile_code,
                    "scope": account_scope.model_dump(mode="json"),
                    "count": count,
                    "extension": extension,
                },
            )

    @staticmethod
    def _metadata(
        profile_code: str | None,
        account_scope: AccountScope,
        booking_from: str | None,
        booking_to: str | None,
        count: int,
        currency: str | None = None,
        frame: pd.DataFrame | None = None,
        ledger_status_counts: dict[str, int] | None = None,
    ) -> dict:
        status_counts = (
            {}
            if frame is None or frame.empty
            else {
                str(key): int(value)
                for key, value in frame["booking_status"].value_counts().items()
            }
        )
        excluded_count = (
            0 if frame is None or frame.empty else int(frame["cashflow_excluded"].sum())
        )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "profile": profile_code or "general",
            "account_scope": account_scope.model_dump(mode="json"),
            "booking_from": booking_from,
            "booking_to": booking_to,
            "transaction_count": count,
            "booking_status_counts": status_counts,
            "ledger_booking_status_counts": ledger_status_counts or {},
            "cashflow_excluded_count": excluded_count,
            "currency_filter": currency.upper() if currency else None,
            "ledger_schema_version": CURRENT_SCHEMA_VERSION,
            "disclaimer": "Local analysis; not tax or investment advice.",
        }

    def _generate_extension(
        self, name: str, frame: pd.DataFrame, metadata: dict, formats: tuple[str, ...]
    ) -> list[Path]:
        config = self.extensions[name]
        scope = AccountScope.model_validate(metadata["account_scope"])
        history = (
            self.dataframe(
                config.profile, account_scope=scope, currency=metadata["currency_filter"]
            )
            if config.include_history
            else None
        )
        metadata["extension"] = {
            "name": name,
            "api_version": REPORT_EXTENSION_API_VERSION,
            "include_history": config.include_history,
        }
        output_dir = (self.output_dir / f"{name}-{uuid4()}").resolve()
        with load_report_extension(config.path) as generate:
            output_dir.mkdir(parents=True)
            context = ReportContext(
                transactions=frame.copy(deep=True),
                history=history,
                metadata=deepcopy(metadata),
                output_dir=output_dir,
                formats=formats,
            )
            paths = validate_report_artifacts(generate(context), output_dir, formats)
        metadata_path = output_dir / "report.metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return [*paths, metadata_path]

    @staticmethod
    def _summary(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=["Kategorie", "Währung", "Betrag"])
        summary = (
            frame.assign(category=frame["category"].fillna("Unclassified"))
            .groupby(["category", "currency"], as_index=False)["cashflow_amount"]
            .sum()
        )
        return pd.DataFrame(summary).rename(
            columns={
                "category": "Kategorie",
                "currency": "Währung",
                "cashflow_amount": "Betrag",
            }
        )

    @staticmethod
    def _monthly_summary(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=["Monat", "Währung", "Einnahmen", "Ausgaben", "Saldo"])
        data = frame.assign(Monat=frame["booking_date"].dt.to_period("M").astype(str))
        result = (
            data.groupby(["Monat", "currency"])["cashflow_amount"]
            .agg(
                Einnahmen=lambda values: values[values > 0].sum(),
                Ausgaben=lambda values: values[values < 0].sum(),
                Saldo="sum",
            )
            .reset_index()
            .rename(columns={"currency": "Währung"})
        )
        return result

    @staticmethod
    def _account_summary(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=["Konto", "Währung", "Einnahmen", "Ausgaben", "Saldo"])
        return (
            frame.groupby(["account_name", "currency"])["cashflow_amount"]
            .agg(
                Einnahmen=lambda values: values[values > 0].sum(),
                Ausgaben=lambda values: values[values < 0].sum(),
                Saldo="sum",
            )
            .reset_index()
            .rename(columns={"account_name": "Konto", "currency": "Währung"})
        )

    @staticmethod
    def _open_items(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=["id", "booking_date", "purpose", "reason"])
        category_missing = frame["category_code"].isna()
        invoice_missing = frame["invoice_number"].isna()
        pending = frame["booking_status"].eq("pending")
        open_rows = frame.loc[
            category_missing | invoice_missing | pending,
            ["id", "booking_date", "purpose"],
        ].copy()
        if open_rows.empty:
            open_rows["reason"] = []
            return open_rows
        reasons = []
        for index in open_rows.index:
            parts = []
            if pending.loc[index]:
                parts.append("Transaction is pending")
            if category_missing.loc[index]:
                parts.append("Category missing")
            if invoice_missing.loc[index]:
                parts.append("Document missing")
            reasons.append("; ".join(parts))
        open_rows["reason"] = reasons
        return open_rows


def _display_table(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(
        columns={
            "Kategorie": "Category",
            "Währung": "Currency",
            "Betrag": "Amount",
            "Monat": "Month",
            "Einnahmen": "Income",
            "Ausgaben": "Expenses",
            "Saldo": "Balance",
            "Konto": "Account",
        }
    )

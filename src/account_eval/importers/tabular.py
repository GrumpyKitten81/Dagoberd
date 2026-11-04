"""Configurable CSV/XLSX import with a small, explicit column mapping."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict, Unpack

from account_eval.importers.base import (
    FileImporter,
    ImportBatch,
    ImportIssue,
    ImportValidationError,
    select_balance_snapshots,
)
from account_eval.ledger.models import (
    ImportBalanceObservation,
    ImportResult,
    ImportRowKind,
    NormalizedTransaction,
    TransactionStatus,
)
from account_eval.ledger.repository import LedgerRepository

DEFAULT_MAPPING = {
    "external_id": ("external_id", "BuchungID"),
    "booking_date": ("booking_date", "fecha", "date", "Datum"),
    "valuta_date": ("valuta_date", "fecha valor", "value_date", "Wertstellung"),
    "amount": ("amount", "importe", "Betrag"),
    "currency": ("currency", "Währung", "waehrung"),
    "purpose": ("purpose", "concepto", "Verwendungszweck", "Beschreibung"),
    "account": ("account", "account_id", "Konto", "Kontonummer"),
    "reference": ("reference", "Referenz", "hash"),
    "counterparty": ("counterparty", "Gegenpartei", "Beguenstigter"),
    "note": ("note", "Notiz"),
    "balance": ("balance", "Saldo", "Stand"),
    "row_kind": ("row_kind", "Art"),
    "booking_status": (
        "booking_status",
        "status",
        "Buchungsstatus",
        "estado",
    ),
}


class TabularOptions(TypedDict, total=False):
    delimiter: str | None
    encoding: str
    sheet_name: str | int
    header_row: int
    default_currency: str
    date_formats: tuple[str, ...]
    decimal_separator: str | None
    skip_purpose_prefixes: tuple[str, ...]
    source_attributes: dict[str, tuple[str, ...]]
    opening_balance_values: tuple[str, ...]


def _value(row: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def parse_amount(value: str, decimal_separator: str | None = None) -> Decimal:
    raw = value.strip().replace("€", "").replace(" ", "")
    if (decimal_separator == "," and "," in raw) or (decimal_separator is None and "," in raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif decimal_separator == ".":
        raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"Invalid monetary amount: {value!r}") from error


def parse_date(
    value: str | date | datetime,
    formats: tuple[str, ...] = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%Y/%m/%d",
    ),
) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = value.strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}")


def parse_booking_status(value: str | None) -> TransactionStatus:
    normalized = "".join(character for character in (value or "").casefold() if character.isalnum())
    aliases = {
        "": TransactionStatus.BOOKED,
        "booked": TransactionStatus.BOOKED,
        "gebucht": TransactionStatus.BOOKED,
        "contabilizado": TransactionStatus.BOOKED,
        "pending": TransactionStatus.PENDING,
        "vorgemerkt": TransactionStatus.PENDING,
        "pendiente": TransactionStatus.PENDING,
        "notbooked": TransactionStatus.PENDING,
        "preliminary": TransactionStatus.PENDING,
        "reversed": TransactionStatus.REVERSED,
        "storniert": TransactionStatus.REVERSED,
        "anulado": TransactionStatus.REVERSED,
        "cancelled": TransactionStatus.REVERSED,
        "corrected": TransactionStatus.CORRECTED,
        "korrigiert": TransactionStatus.CORRECTED,
        "corregido": TransactionStatus.CORRECTED,
    }
    if normalized not in aliases:
        raise ValueError(f"Invalid booking status: {value!r}")
    return aliases[normalized]


def _inspect_rows(
    rows: Iterable[dict[str, object]],
    mapping: dict[str, tuple[str, ...]] = DEFAULT_MAPPING,
    *,
    default_currency: str = "EUR",
    date_formats: tuple[str, ...] = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%Y/%m/%d",
    ),
    decimal_separator: str | None = None,
    skip_purpose_prefixes: tuple[str, ...] = (),
    source_attributes: dict[str, tuple[str, ...]] | None = None,
    opening_balance_values: tuple[str, ...] = ("opening_balance", "Eröffnungssaldo"),
) -> ImportBatch:
    output: list[NormalizedTransaction] = []
    balance_observations: list[ImportBalanceObservation] = []
    issues: list[ImportIssue] = []
    total = 0
    for index, row in enumerate(rows, start=1):
        total = index
        booking = _value(row, mapping.get("booking_date", ()))
        amount = _value(row, mapping.get("amount", ()))
        purpose = _value(row, mapping.get("purpose", ()))
        if booking and booking.casefold() in {"offen", "open", "--"}:
            continue
        if purpose is None:
            purpose = _value(row, mapping.get("counterparty", ()))
        if purpose and any(purpose.startswith(prefix) for prefix in skip_purpose_prefixes):
            continue
        if booking is None or amount is None or purpose is None:
            for field, value in (
                ("booking_date", booking),
                ("amount", amount),
                ("purpose", purpose),
            ):
                if not value:
                    issues.append(
                        ImportIssue(
                            index,
                            field,
                            "required",
                            f"Zeile {index}: Pflichtfeld {field} is missing.",
                        )
                    )
            continue
        reference = _value(row, mapping.get("reference", ()))
        explicit_external_id = _value(row, mapping.get("external_id", ()))
        valuta = _value(row, mapping.get("valuta_date", ()))
        try:
            parsed_booking = parse_date(booking, date_formats)
            parsed_valuta = parse_date(valuta, date_formats) if valuta else None
            parsed_amount = parse_amount(amount, decimal_separator)
            booking_status = parse_booking_status(_value(row, mapping.get("booking_status", ())))
        except ValueError as error:
            issues.append(ImportIssue(index, None, "invalid_value", str(error)))
            continue
        currency = (_value(row, mapping.get("currency", ())) or default_currency).upper()
        account = _value(row, mapping.get("account", ())) or "file-import"
        raw_row = {
            str(key): "" if value is None else str(value)
            for key, value in row.items()
            if key is not None and str(key).strip()
        }
        attributes = {
            key: value
            for key, columns in (source_attributes or {}).items()
            if (value := _value(row, columns)) is not None
        }
        raw_row_kind = _value(row, mapping.get("row_kind", ()))
        opening_values = {value.casefold() for value in opening_balance_values}
        row_kind = (
            ImportRowKind.OPENING_BALANCE
            if raw_row_kind and raw_row_kind.casefold() in opening_values
            else ImportRowKind.TRANSACTION
        )
        identity_payload = {
            "account": account,
            "booking_date": parsed_booking.isoformat(),
            "valuta_date": parsed_valuta.isoformat() if parsed_valuta else None,
            "amount": str(parsed_amount),
            "currency": currency,
            "purpose": purpose,
            "counterparty": _value(row, mapping.get("counterparty", ())),
            "reference": reference,
        }
        external_id = (
            explicit_external_id
            or reference
            or hashlib.sha256(
                json.dumps(identity_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        )
        balance_value = _value(row, mapping.get("balance", ()))
        observed_balance: Decimal | None = None
        if balance_value is not None:
            try:
                observed_balance = parse_amount(balance_value, decimal_separator)
            except ValueError as error:
                issues.append(ImportIssue(index, "balance", "invalid_value", str(error)))
                continue
            balance_observations.append(
                ImportBalanceObservation(
                    external_id=external_id,
                    account_external_id=account,
                    balance_date=parsed_valuta or parsed_booking,
                    balance=observed_balance,
                    currency=currency,
                    source_row=index,
                    row_kind=row_kind,
                    source_attributes=attributes,
                    raw=raw_row,
                )
            )
        if row_kind is ImportRowKind.OPENING_BALANCE:
            if observed_balance is None:
                issues.append(
                    ImportIssue(
                        index,
                        "balance",
                        "required_opening_balance",
                        f"Zeile {index}: opening balance requires a balance.",
                    )
                )
            continue
        output.append(
            NormalizedTransaction(
                external_id=external_id,
                booking_date=parsed_booking,
                valuta_date=parsed_valuta,
                amount=parsed_amount,
                currency=currency,
                purpose=purpose,
                account_external_id=account,
                counterparty=identity_payload["counterparty"],
                reference=reference,
                note=_value(row, mapping.get("note", ())),
                source_attributes=attributes,
                observed_balance=observed_balance,
                row_kind=row_kind,
                raw=raw_row,
                booking_status=booking_status,
            )
        )
    observations = tuple(balance_observations)
    return ImportBatch(
        tuple(output),
        tuple(issues),
        total,
        observations,
        select_balance_snapshots(observations),
    )


def read_tabular_file(
    path: str | Path,
    mapping: dict[str, tuple[str, ...]] = DEFAULT_MAPPING,
    **options: Unpack[TabularOptions],
) -> list[NormalizedTransaction]:
    batch = inspect_tabular_file(path, mapping, **options)
    if batch.issues:
        raise ImportValidationError(batch.issues)
    return list(batch.transactions)


def inspect_tabular_file(
    path: str | Path,
    mapping: dict[str, tuple[str, ...]] = DEFAULT_MAPPING,
    *,
    delimiter: str | None = None,
    encoding: str = "utf-8-sig",
    sheet_name: str | int = 0,
    header_row: int = 0,
    default_currency: str = "EUR",
    date_formats: tuple[str, ...] = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%Y/%m/%d",
    ),
    decimal_separator: str | None = None,
    skip_purpose_prefixes: tuple[str, ...] = (),
    source_attributes: dict[str, tuple[str, ...]] | None = None,
    opening_balance_values: tuple[str, ...] = ("opening_balance", "Eröffnungssaldo"),
) -> ImportBatch:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding=encoding, newline="") as handle:
            preamble = [handle.readline() for _ in range(header_row)]
            sample = "".join(preamble) + handle.read(4096)
            handle.seek(0)
            for _ in range(header_row):
                handle.readline()
            selected_delimiter = delimiter
            if selected_delimiter is None:
                try:
                    selected_delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
                except csv.Error:
                    return ImportBatch(
                        (),
                        (
                            ImportIssue(
                                None,
                                None,
                                "delimiter_unknown",
                                "CSV delimiter could not be detected.",
                            ),
                        ),
                    )
            return _inspect_rows(
                csv.DictReader(handle, delimiter=selected_delimiter),
                mapping,
                default_currency=default_currency,
                date_formats=date_formats,
                decimal_separator=decimal_separator,
                skip_purpose_prefixes=skip_purpose_prefixes,
                source_attributes=source_attributes,
                opening_balance_values=opening_balance_values,
            )
    if path.suffix.lower() in {".xlsx", ".xls"}:
        import pandas as pd

        try:
            frame = pd.read_excel(
                path,
                dtype=str,
                sheet_name=sheet_name,
                header=header_row,
            ).fillna("")
        except (ValueError, IndexError) as error:
            return ImportBatch((), (ImportIssue(None, None, "sheet_not_found", str(error)),))
        records = [
            {str(key): value for key, value in record.items()}
            for record in frame.to_dict("records")
        ]
        return _inspect_rows(
            records,
            mapping,
            default_currency=default_currency,
            date_formats=date_formats,
            decimal_separator=decimal_separator,
            skip_purpose_prefixes=skip_purpose_prefixes,
            source_attributes=source_attributes,
            opening_balance_values=opening_balance_values,
        )
    return ImportBatch(
        (),
        (ImportIssue(None, None, "unsupported_format", "CSV, XLSX and XLS files are supported."),),
    )


class TabularImporter(FileImporter):
    source = "tabular"
    provider_name = "Dateiimport"

    def __init__(
        self,
        mapping: dict[str, tuple[str, ...]] | None = None,
        *,
        delimiter: str | None = None,
        encoding: str = "utf-8-sig",
        sheet_name: str | int = 0,
        header_row: int = 0,
        default_currency: str = "EUR",
        date_formats: tuple[str, ...] | None = None,
        decimal_separator: str | None = None,
        skip_purpose_prefixes: tuple[str, ...] = (),
        source_attributes: dict[str, tuple[str, ...]] | None = None,
        opening_balance_values: tuple[str, ...] = ("opening_balance", "Eröffnungssaldo"),
    ) -> None:
        self.mapping = {**DEFAULT_MAPPING, **(mapping or {})}
        self.options: TabularOptions = {
            "delimiter": delimiter,
            "encoding": encoding,
            "sheet_name": sheet_name,
            "header_row": header_row,
            "default_currency": default_currency,
            "date_formats": date_formats or ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"),
            "decimal_separator": decimal_separator,
            "skip_purpose_prefixes": skip_purpose_prefixes,
            "source_attributes": source_attributes or {},
            "opening_balance_values": opening_balance_values,
        }
        self.version = (
            "tabular-v3-"
            + hashlib.sha256(
                json.dumps(
                    {"mapping": self.mapping, "options": self.options},
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:12]
        )

    def read_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> list[NormalizedTransaction]:
        transactions = read_tabular_file(path, self.mapping, **self.options)
        if not account_external_id:
            return transactions
        return [
            replace(transaction, account_external_id=account_external_id)
            for transaction in transactions
        ]

    def inspect_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> ImportBatch:
        batch = inspect_tabular_file(path, self.mapping, **self.options)
        if not account_external_id:
            return batch
        return ImportBatch(
            tuple(
                replace(item, account_external_id=account_external_id)
                for item in batch.transactions
            ),
            batch.issues,
            batch.rows_total,
            tuple(
                replace(item, account_external_id=account_external_id)
                for item in batch.balance_observations
            ),
            tuple(
                replace(item, account_external_id=account_external_id)
                for item in batch.balance_snapshots
            ),
        )


def import_tabular_file(
    repository: LedgerRepository, path: str | Path, provider_name: str = "Dateiimport"
) -> ImportResult:
    return repository.import_transactions(
        "file", provider_name, TabularImporter().read_transactions(path)
    )

"""ISO 20022 CAMT.053 statement importer with deterministic identities."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar
from xml.etree import ElementTree

from dagoberd.importers.base import FileImporter, ImportBatch, ImportIssue
from dagoberd.ledger.models import NormalizedTransaction, TransactionStatus


class Camt053Importer(FileImporter):
    source = "camt053"
    version = "camt053-v1"
    provider_kind = "camt053"
    provider_name = "CAMT.053"
    supported_suffixes: ClassVar[set[str]] = {".xml", ".camt", ".053"}
    max_file_size = 50 * 1024 * 1024

    def read_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> list[NormalizedTransaction]:
        batch = self.inspect_transactions(path, account_external_id)
        if batch.issues:
            from dagoberd.importers.base import ImportValidationError

            raise ImportValidationError(batch.issues)
        return list(batch.transactions)

    def inspect_transactions(
        self,
        path: str | Path,
        account_external_id: str | None = None,
    ) -> ImportBatch:
        file_path = Path(path)
        if file_path.suffix.casefold() not in self.supported_suffixes:
            return ImportBatch(
                (),
                (
                    ImportIssue(
                        None,
                        None,
                        "unsupported_format",
                        "CAMT.053 requires an XML/CAMT/053 file.",
                    ),
                ),
            )
        if file_path.stat().st_size > self.max_file_size:
            return ImportBatch(
                (),
                (ImportIssue(None, None, "file_too_large", "CAMT.053 file exceeds 50 MiB."),),
            )
        raw_bytes = file_path.read_bytes()
        probe = raw_bytes.upper()
        if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
            return ImportBatch(
                (),
                (ImportIssue(None, None, "unsafe_xml", "DTD and XML entities are not allowed."),),
            )
        try:
            root = ElementTree.fromstring(raw_bytes)
        except ElementTree.ParseError as error:
            return ImportBatch(
                (), (ImportIssue(None, None, "invalid_xml", f"Invalid XML: {error}"),)
            )

        transactions: list[NormalizedTransaction] = []
        issues: list[ImportIssue] = []
        unidentified_account = False
        entries = root.findall(".//{*}Stmt/{*}Ntry")
        row_number = 0
        for statement in root.findall(".//{*}Stmt"):
            xml_account = self._text(statement, "./{*}Acct/{*}Id/{*}IBAN") or self._text(
                statement, "./{*}Acct/{*}Id/{*}Othr/{*}Id"
            )
            if account_external_id and not xml_account:
                unidentified_account = True
            if account_external_id and xml_account and xml_account != account_external_id:
                issues.append(
                    ImportIssue(
                        None,
                        "account",
                        "account_mismatch",
                        "Source account identity differs from the selected account; review the scope.",
                    )
                )
            selected_account = account_external_id or xml_account
            statement_entries = statement.findall("./{*}Ntry")
            for entry in statement_entries:
                row_number += 1
                try:
                    transactions.append(self._transaction(entry, selected_account, row_number))
                except ValueError as error:
                    issues.append(ImportIssue(row_number, None, "invalid_entry", str(error)))
        if not entries and not issues:
            issues.append(ImportIssue(None, None, "no_entries", "No CAMT.053 transactions found."))
        return ImportBatch(
            tuple(transactions),
            tuple(issues),
            row_number,
            unidentified_account=unidentified_account,
        )

    def _transaction(
        self,
        entry: ElementTree.Element,
        account_external_id: str | None,
        row_number: int,
    ) -> NormalizedTransaction:
        if not account_external_id:
            raise ValueError(f"CAMT.053-Zeile {row_number}: account ID/IBAN is missing.")
        amount_node = entry.find("./{*}Amt")
        amount_text = (
            amount_node.text.strip() if amount_node is not None and amount_node.text else ""
        )
        currency = amount_node.attrib.get("Ccy", "EUR") if amount_node is not None else "EUR"
        booking_text = self._text(entry, "./{*}BookgDt/{*}Dt") or self._text(
            entry, "./{*}BookgDt/{*}DtTm"
        )
        if not amount_text or not booking_text:
            raise ValueError(f"CAMT.053-Zeile {row_number}: booking date or amount is missing.")
        sign = (
            Decimal("-1")
            if (self._text(entry, "./{*}CdtDbtInd") or "").upper() == "DBIT"
            else Decimal("1")
        )
        amount = Decimal(amount_text) * sign
        valuta_text = self._text(entry, "./{*}ValDt/{*}Dt") or self._text(
            entry, "./{*}ValDt/{*}DtTm"
        )
        details = entry.find("./{*}NtryDtls/{*}TxDtls")
        purpose = (
            self._first_text(
                details,
                (
                    ".//{*}RmtInf/{*}Ustrd",
                    ".//{*}AddtlTxInf",
                    ".//{*}Cdtr/{*}Nm",
                    ".//{*}Dbtr/{*}Nm",
                ),
            )
            or self._text(entry, "./{*}AddtlNtryInf")
            or "CAMT.053 transaction"
        )
        counterparty = self._first_text(
            details,
            (
                ".//{*}Cdtr/{*}Nm",
                ".//{*}Dbtr/{*}Nm",
            ),
        )
        reference = self._first_text(
            details,
            (
                ".//{*}Refs/{*}EndToEndId",
                ".//{*}Refs/{*}TxId",
                ".//{*}Refs/{*}MndtId",
            ),
        )
        bank_reference = self._text(entry, "./{*}AcctSvcrRef") or self._text(entry, "./{*}NtryRef")
        canonical = {
            "account": account_external_id,
            "booking_date": booking_text[:10],
            "valuta_date": valuta_text[:10] if valuta_text else None,
            "amount": str(amount),
            "currency": currency.upper(),
            "purpose": purpose,
            "reference": reference,
            "bank_reference": bank_reference,
        }
        external_id = (
            bank_reference
            or hashlib.sha256(
                json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        )
        status_text = (
            self._text(entry, "./{*}Sts/{*}Cd") or self._text(entry, "./{*}Sts") or "BOOK"
        ).upper()
        status = (
            TransactionStatus.BOOKED
            if status_text in {"BOOK", "BOOKED"}
            else TransactionStatus.PENDING
        )
        return NormalizedTransaction(
            external_id=external_id,
            booking_date=date.fromisoformat(booking_text[:10]),
            valuta_date=date.fromisoformat(valuta_text[:10]) if valuta_text else None,
            amount=amount,
            currency=currency.upper(),
            purpose=purpose,
            account_external_id=account_external_id,
            counterparty=counterparty,
            reference=reference or bank_reference,
            raw=canonical,
            booking_status=status,
        )

    @staticmethod
    def _text(node: ElementTree.Element | None, path: str) -> str | None:
        if node is None:
            return None
        found = node.find(path)
        return (
            found.text.strip() if found is not None and found.text and found.text.strip() else None
        )

    @classmethod
    def _first_text(cls, node: ElementTree.Element | None, paths: tuple[str, ...]) -> str | None:
        for path in paths:
            value = cls._text(node, path)
            if value:
                return value
        return None

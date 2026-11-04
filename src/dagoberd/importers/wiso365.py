"""WISO Mein Geld 365 workbook adapter without user-specific mappings."""

from __future__ import annotations

from dagoberd.importers.tabular import TabularImporter

WISO365_MAPPING: dict[str, tuple[str, ...]] = {
    "external_id": ("BuchungID",),
    "booking_date": ("Buchungsdatum",),
    "valuta_date": ("Wertstellung",),
    "amount": ("Betrag",),
    "currency": ("Währung",),
    "purpose": (
        "Verwendungszweck",
        "Vorgang",
        "Kategorie",
        "Empfänger/Auftraggeber",
    ),
    "account": ("Kontoname",),
    "reference": ("End to End ID",),
    "counterparty": ("Empfänger/Auftraggeber",),
    "note": ("Notiz",),
    "balance": ("Stand [€]", "Stand"),
    "row_kind": ("Art",),
    # WISO's boolean Status is source metadata, not a booking lifecycle state.
    "booking_status": (),
}

WISO365_SOURCE_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "account_identifier": ("IBAN/KontoNr",),
    "source_category": ("Kategorie",),
    "tax": ("Steuer",),
    "free_1": ("Frei 1",),
    "free_2": ("Frei 2",),
    "free_3": ("Frei 3",),
    "contract": ("Vertrag",),
    "documents": ("Dokumente",),
    "source_status": ("Status",),
    "tax_relevance": ("Steuerrelevanz",),
    "transaction_type": ("Art",),
    "booking_text": ("Buchungstext",),
    "bank_code": ("BIC/BLZ",),
    "statement_number": ("Kontoauszugs-Nr.",),
    "operation": ("Vorgang",),
    "creditor_id": ("Gläubiger ID",),
    "originator_id": ("Originator ID",),
}


class Wiso365Importer(TabularImporter):
    """Read the standard multi-account WISO 365 Excel export."""

    source = "wiso365"
    provider_name = "WISO Mein Geld 365"

    def __init__(self) -> None:
        super().__init__(
            WISO365_MAPPING,
            default_currency="EUR",
            date_formats=("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"),
            decimal_separator=",",
            source_attributes=WISO365_SOURCE_ATTRIBUTES,
            opening_balance_values=("Eröffnungssaldo",),
        )
        self.version = self.version.replace("tabular-v3", "wiso365-v2", 1)

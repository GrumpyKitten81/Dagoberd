from datetime import date, datetime
from decimal import Decimal

from openpyxl import Workbook

from account_eval.config import ImportAnnotationConfig, ImportProfileConfig
from account_eval.importers.tabular import parse_date, read_tabular_file
from account_eval.importers.wiso365 import Wiso365Importer
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository
from account_eval.services.import_service import ImportService


def test_zero_amount_import_creates_review_warning(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "file",
        "Synthetic",
        [
            NormalizedTransaction(
                "zero",
                date(2026, 8, 1),
                Decimal("0"),
                "EUR",
                "Synthetic zero amount",
                "account-zero",
            )
        ],
    )

    with repository.database.connect() as connection:
        review = connection.execute(
            """SELECT item_type, reason FROM ledger_review_items
               WHERE reason='Review zero transaction from source file'"""
        ).fetchone()
    assert review is not None
    assert review["item_type"] == "import_warning"


def test_external_transfer_annotation_creates_suggestion_not_confirmation(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    result = repository.import_transactions(
        "file",
        "Synthetic",
        [
            NormalizedTransaction(
                "out",
                date(2026, 8, 1),
                Decimal("-25"),
                "EUR",
                "Transfer out",
                "account-a",
                source_attributes={"transaction_type": "Externe Umbuchung"},
            ),
            NormalizedTransaction(
                "in",
                date(2026, 8, 2),
                Decimal("25"),
                "EUR",
                "Transfer in",
                "account-b",
            ),
        ],
    )

    assert result.transfer_candidates == 1
    assert result.transfer_relations_suggested == 1
    relation = repository.list_transaction_relations(relation_type="transfer")[0]
    assert relation["status"] == "suggested"


def test_parse_date_accepts_excel_datetime_strings():
    assert parse_date("2022-02-27 00:00:00").isoformat() == "2022-02-27"
    assert parse_date(datetime(2022, 2, 27, 0, 0, 0)).isoformat() == "2022-02-27"


def test_csv_import_uses_counterparty_when_purpose_missing(tmp_path):
    file = tmp_path / "transactions.csv"
    file.write_text(
        "Datum;Betrag;Verwendungszweck;Empfänger\n2026-01-02;10,00;;Bar Einzahlung\n",
        encoding="utf-8",
    )
    mapping = {
        "booking_date": ("Datum",),
        "amount": ("Betrag",),
        "purpose": ("Verwendungszweck",),
        "counterparty": ("Empfänger",),
        "reference": ("Referenz",),
        "valuta_date": ("Valuta",),
        "currency": ("Währung",),
        "account": ("Konto",),
        "booking_status": ("Status",),
    }
    transactions = read_tabular_file(file, mapping=mapping, delimiter=";")
    assert transactions[0].purpose == "Bar Einzahlung"


def test_csv_import_normalises_german_amount_and_date(tmp_path):
    file = tmp_path / "transactions.csv"
    file.write_text(
        "fecha;concepto;fecha valor;importe;saldo\n01/07/2026;Miete;01/07/2026;1.234,56;0\n",
        encoding="utf-8",
    )
    transactions = read_tabular_file(file)
    assert str(transactions[0].amount) == "1234.56"
    assert transactions[0].booking_date.isoformat() == "2026-07-01"


def test_csv_import_normalises_spanish_pending_status(tmp_path):
    file = tmp_path / "transactions.csv"
    file.write_text(
        "fecha;concepto;importe;estado\n01/07/2026;Tarjeta;-12,34;pendiente\n",
        encoding="utf-8",
    )

    transactions = read_tabular_file(file)

    assert transactions[0].booking_status == "pending"


def test_wiso365_import_preserves_identity_attributes_note_and_balances(tmp_path):
    path = tmp_path / "wiso.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "BuchungID",
            "Kontoname",
            "Buchungsdatum",
            "Wertstellung",
            "Betrag",
            "Währung",
            "Verwendungszweck",
            "Empfänger/Auftraggeber",
            "Art",
            "Vorgang",
            "Stand [€]",
            "Status",
            "Notiz",
            "End to End ID",
            "Kategorie",
            "Steuer",
        ]
    )
    sheet.append(
        [
            "opening-1",
            "Synthetic Cash",
            "01.01.2026",
            "01.01.2026",
            100.0,
            "EUR",
            "Opening",
            "",
            "Eröffnungssaldo",
            "",
            100.0,
            "False",
            "",
            "",
            "",
            "",
        ]
    )
    sheet.append(
        [
            "booking-1",
            "Synthetic Cash",
            "02.01.2026",
            "02.01.2026",
            -10.0,
            "EUR",
            "",
            "Synthetic Shop",
            "Kartenzahlung",
            "Purchase",
            90.0,
            "False",
            "Imported note",
            "E2E-1",
            "Synthetic WISO category",
            "Synthetic tax value",
        ]
    )
    workbook.save(path)

    batch = Wiso365Importer().inspect_transactions(path)

    assert batch.rows_total == 2
    assert len(batch.transactions) == 1
    assert batch.transactions[0].external_id == "booking-1"
    assert batch.transactions[0].reference == "E2E-1"
    assert batch.transactions[0].amount == Decimal("-10.0")
    assert batch.transactions[0].purpose == "Purchase"
    assert batch.transactions[0].note == "Imported note"
    assert batch.transactions[0].source_attributes["source_status"] == "False"
    assert len(batch.balance_observations) == 2
    assert {role for item in batch.balance_snapshots for role in item.roles} == {
        "opening",
        "month_end",
        "closing",
    }

    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    service = ImportService(
        repository,
        profiles={
            "synthetic-wiso": ImportProfileConfig(
                source="wiso365",
                annotations=ImportAnnotationConfig(
                    category_column="Kategorie",
                    category_profile="wiso",
                    profile_columns={"Steuer": "wiso-tax"},
                    preserve_unknown_categories=True,
                ),
            )
        },
    )
    preview = service.preview("wiso365", path, profile="synthetic-wiso")
    execution = service.execute(preview.preview_id)

    assert execution.imported == 1
    assert execution.balance_snapshots == 2
    assert execution.annotations_applied == 2
    transaction = repository.list_transactions()[0]
    detail = repository.transaction_detail(transaction["id"])
    assert detail["note"] == "Imported note"
    assert detail["source_attributes"]["source_status"] == "False"
    assert repository.list_balance_snapshots(account_id=transaction["account_id"])
    with repository.database.connect() as connection:
        preserved = connection.execute(
            """SELECT p.code, c.name FROM ledger_profile_categories pc
               JOIN ledger_profiles p ON p.id=pc.profile_id
               JOIN ledger_categories c ON c.id=pc.category_id
               WHERE p.code IN ('wiso', 'wiso-tax') ORDER BY p.code"""
        ).fetchall()
    assert [(row["code"], row["name"]) for row in preserved] == [
        ("wiso", "Synthetic WISO category"),
        ("wiso-tax", "Synthetic tax value"),
    ]


def test_explicit_external_id_precedes_reference_and_content_hash(tmp_path):
    path = tmp_path / "transactions.csv"
    path.write_text(
        "Datum;Betrag;Zweck;ID;Referenz\n01.01.2026;1,00;Synthetic;id-1;ref-1\n",
        encoding="utf-8",
    )
    mapping = {
        "external_id": ("ID",),
        "booking_date": ("Datum",),
        "amount": ("Betrag",),
        "purpose": ("Zweck",),
        "reference": ("Referenz",),
    }

    transaction = read_tabular_file(path, mapping, delimiter=";")[0]

    assert transaction.external_id == "id-1"
    assert transaction.reference == "ref-1"
    assert transaction.amount == Decimal("1.00")

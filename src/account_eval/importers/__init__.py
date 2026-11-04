"""File import adapters for the provider-neutral ledger."""

from pathlib import Path

from account_eval.ledger.models import ImportResult
from account_eval.ledger.repository import LedgerRepository

from .base import FileImporter
from .camt053 import Camt053Importer
from .tabular import TabularImporter, import_tabular_file, read_tabular_file
from .wiso365 import Wiso365Importer

IMPORTERS: dict[str, FileImporter] = {
    Camt053Importer.source: Camt053Importer(),
    TabularImporter.source: TabularImporter(),
    Wiso365Importer.source: Wiso365Importer(),
}


def available_sources() -> tuple[str, ...]:
    return tuple(IMPORTERS)


def get_importer(source: str) -> FileImporter:
    try:
        return IMPORTERS[source]
    except KeyError as error:
        raise ValueError(f"Unbekannte Importquelle: {source}.") from error


def import_file(
    repository: LedgerRepository,
    source: str,
    path: str | Path,
    account_external_id: str | None = None,
) -> ImportResult:
    importer = get_importer(source)
    transactions = importer.read_transactions(path, account_external_id)
    return repository.import_transactions(
        importer.provider_kind,
        importer.provider_name,
        transactions,
    )


__all__ = [
    "Camt053Importer",
    "FileImporter",
    "TabularImporter",
    "Wiso365Importer",
    "available_sources",
    "get_importer",
    "import_file",
    "import_tabular_file",
    "read_tabular_file",
]

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dagoberd.documents.batch import DocumentBatchService
from dagoberd.documents.extraction import create_local_ocr_provider
from dagoberd.documents.matching import DocumentMatcher
from dagoberd.ledger.database import LedgerDatabase
from dagoberd.ledger.models import NormalizedTransaction
from dagoberd.ledger.repository import LedgerRepository


def test_document_matching_keeps_ambiguous_candidate_as_suggestion(tmp_path):
    repo = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repo.import_transactions(
        "file",
        "test",
        [
            NormalizedTransaction(
                "one",
                date(2026, 7, 2),
                Decimal("-42.00"),
                "EUR",
                "Electricidad ACME",
                "one",
                reference="invoice-1",
            )
        ],
    )
    document_id = DocumentMatcher(repo).add_document(
        "acme.pdf", Decimal("42.00"), date(2026, 7, 2), "ACME"
    )
    matches = DocumentMatcher(repo).suggest(document_id)
    assert matches[0]["score"] == 0.9
    assert matches[0]["status"] == "suggested"


def test_document_import_stores_account_files_in_a_separate_directory(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "file",
        "Bankexport",
        [
            NormalizedTransaction(
                "one",
                date(2026, 7, 2),
                Decimal("-42.00"),
                "EUR",
                "Electricidad ACME",
                "account-one",
            )
        ],
    )
    account_id = repository.list_accounts()[0]["id"]
    document = tmp_path / "invoice.txt"
    document.write_text("local document", encoding="utf-8")

    imported = DocumentMatcher(repository, tmp_path / "documents").import_file(
        document,
        account_id=account_id,
    )

    assert Path(imported["storage_path"]).parent == tmp_path / "documents" / account_id


def test_manual_document_assignment_marks_document_matched(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "file",
        "Bankexport",
        [
            NormalizedTransaction(
                "one", date(2026, 7, 2), Decimal("-42.00"), "EUR", "Electricity", "account-one"
            )
        ],
    )
    transaction = repository.list_transactions()[0]
    document_id = DocumentMatcher(repository).add_document("invoice.pdf", Decimal("42.00"))

    result = DocumentMatcher(repository).assign(document_id, transaction["id"])

    assert result["status"] == "matched"
    assert DocumentMatcher(repository).list_documents(status="matched")[0]["id"] == document_id


def test_document_batch_deduplicates_content_but_preserves_every_origin(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"\xff\xd8\xffsynthetic-one")
    (source / "copy.jpeg").write_bytes(b"\xff\xd8\xffsynthetic-one")
    (source / "two.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-two")
    service = DocumentBatchService(repository, tmp_path / "documents")

    preview = service.preview(source)
    result = service.execute(preview.preview_id, preview.fingerprint)

    assert preview.valid_count == 3
    assert result.created_documents == 2
    assert result.created_origins == 3
    with repository.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM ledger_documents").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM ledger_document_origins").fetchone()[0] == 3


def test_document_batch_isolates_mime_spoofing_and_keeps_valid_files(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "valid.jpg").write_bytes(b"\xff\xd8\xffsynthetic-image")
    (source / "spoofed.pdf").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-image")
    service = DocumentBatchService(repository, tmp_path / "documents")

    preview = service.preview(source)
    result = service.execute(preview.preview_id, preview.fingerprint)

    assert preview.valid_count == 1
    assert preview.error_count == 1
    assert result.created_documents == 1
    assert result.errors[0]["relative_path"] == "spoofed.pdf"
    assert "extension does not match" in result.errors[0]["error"]


def test_document_batch_rejects_a_file_changed_after_preview(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    source = tmp_path / "receipt.jpg"
    source.write_bytes(b"\xff\xd8\xffbefore")
    service = DocumentBatchService(repository, tmp_path / "documents")
    preview = service.preview(source)
    source.write_bytes(b"\xff\xd8\xffafter")

    with pytest.raises(ValueError, match="changed after preview"):
        service.execute(preview.preview_id, preview.fingerprint)

    assert service.get(preview.preview_id).status == "failed"


def test_document_batch_rejects_paths_resolving_outside_source(
    tmp_path: Path, create_symlink: Callable[[Path, Path], None]
) -> None:
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffoutside")
    create_symlink(source / "linked.jpg", outside)
    service = DocumentBatchService(repository, tmp_path / "documents")

    preview = service.preview(source)

    assert preview.error_count == 1
    assert "escapes" in preview.files[0].errors[0]


def test_document_match_preview_is_read_only_and_supports_append_links(tmp_path):
    repository = LedgerRepository(LedgerDatabase(tmp_path / "ledger.db"))
    repository.import_transactions(
        "file",
        "Bankexport",
        [
            NormalizedTransaction(
                "one", date(2026, 7, 2), Decimal("-42.00"), "EUR", "ACME one", "account-one"
            ),
            NormalizedTransaction(
                "two", date(2026, 7, 3), Decimal("-42.00"), "EUR", "ACME two", "account-one"
            ),
        ],
    )
    matcher = DocumentMatcher(repository)
    document_id = matcher.add_document("invoice.pdf", Decimal("42.00"), merchant="ACME")

    preview = matcher.preview_matches(document_id)
    with repository.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM ledger_document_matches").fetchone()[0] == 0
    selected = [item["transaction_id"] for item in preview["candidates"]]
    result = matcher.apply_match_preview(
        document_id,
        preview["fingerprint"],
        selected,
        expected_document_version=preview["document_version"],
        append=True,
    )

    assert len(result["matches"]) == 2
    with repository.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM ledger_document_matches WHERE status='matched'"
            ).fetchone()[0]
            == 2
        )


def test_local_ocr_is_optional_and_missing_executable_is_explicit(monkeypatch):
    assert (
        create_local_ocr_provider(
            enabled=False,
            executable="missing-tesseract",
            pdf_renderer="missing-renderer",
            languages=("deu",),
            timeout_seconds=30,
        )
        is None
    )
    monkeypatch.setattr("dagoberd.documents.extraction.shutil.which", lambda _value: None)
    with pytest.raises(ValueError, match="Local OCR executable is not available"):
        create_local_ocr_provider(
            enabled=True,
            executable="missing-tesseract",
            pdf_renderer="missing-renderer",
            languages=("deu",),
            timeout_seconds=30,
        )

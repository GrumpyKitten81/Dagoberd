"""Local document validation and metadata extraction."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

SUPPORTED_DOCUMENT_EXTENSIONS = frozenset(
    {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".docx"}
)


class LocalOCRProvider(Protocol):
    """Local-only OCR boundary supplied by the application owner."""

    def extract_text(self, path: Path, mime_type: str) -> str:
        """Return text without sending document content to a remote service."""


class TesseractOCRProvider:
    """Optional local OCR implementation backed by command-line tools."""

    def __init__(
        self,
        *,
        executable: str = "tesseract",
        pdf_renderer: str = "pdftoppm",
        languages: tuple[str, ...] = ("deu", "eng"),
        timeout_seconds: int = 120,
        max_pdf_pages: int = 5,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise ValueError(f"Local OCR executable is not available: {executable}")
        self.executable = resolved
        self.pdf_renderer = shutil.which(pdf_renderer)
        self.languages = languages
        self.timeout_seconds = timeout_seconds
        self.max_pdf_pages = max_pdf_pages

    def extract_text(self, path: Path, mime_type: str) -> str:
        if mime_type == "application/pdf":
            return self._extract_pdf(path)
        if not mime_type.startswith("image/"):
            return ""
        return self._run_tesseract(path)

    def _extract_pdf(self, path: Path) -> str:
        if self.pdf_renderer is None:
            raise ValueError("Local PDF OCR requires the pdftoppm executable")
        with tempfile.TemporaryDirectory(prefix="account-eval-ocr-") as directory:
            prefix = Path(directory) / "page"
            subprocess.run(
                [
                    self.pdf_renderer,
                    "-f",
                    "1",
                    "-l",
                    str(self.max_pdf_pages),
                    "-r",
                    "200",
                    "-png",
                    str(path),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
            return "\n".join(
                self._run_tesseract(page) for page in sorted(Path(directory).glob("page-*.png"))
            )

    def _run_tesseract(self, path: Path) -> str:
        command = [self.executable, str(path), "stdout"]
        if self.languages:
            command.extend(["-l", "+".join(self.languages)])
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        return result.stdout


def create_local_ocr_provider(
    *,
    enabled: bool,
    executable: str,
    pdf_renderer: str,
    languages: tuple[str, ...],
    timeout_seconds: int,
) -> LocalOCRProvider | None:
    """Construct the configured provider without ever selecting a remote service."""
    if not enabled:
        return None
    return TesseractOCRProvider(
        executable=executable,
        pdf_renderer=pdf_renderer,
        languages=languages,
        timeout_seconds=timeout_seconds,
    )


def detect_mime_type(path: Path) -> str:
    """Identify supported formats from their content, not their filename."""
    header = path.read_bytes()[:1024]
    if header.lstrip(b"\x00\t\r\n ").startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raise ValueError("Unsupported or unrecognized document content")


def expected_mime_types(path: Path) -> frozenset[str]:
    suffix = path.suffix.casefold()
    return {
        ".pdf": frozenset({"application/pdf"}),
        ".jpg": frozenset({"image/jpeg"}),
        ".jpeg": frozenset({"image/jpeg"}),
        ".png": frozenset({"image/png"}),
        ".tif": frozenset({"image/tiff"}),
        ".tiff": frozenset({"image/tiff"}),
        ".docx": frozenset(
            {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        ),
    }.get(suffix, frozenset())


def extract_metadata(
    path: Path,
    mime_type: str,
    *,
    ocr_provider: LocalOCRProvider | None = None,
) -> tuple[dict[str, object], str]:
    text = ""
    extraction_status = "no_metadata"
    if mime_type == "application/pdf":
        text = _extract_pdf_text(path)
        extraction_status = "extracted" if text.strip() else "no_metadata"
        if not text.strip() and ocr_provider is not None:
            text = ocr_provider.extract_text(path, mime_type)
            extraction_status = "ocr" if text.strip() else "no_metadata"
    elif mime_type.startswith("image/"):
        if ocr_provider is not None:
            text = ocr_provider.extract_text(path, mime_type)
            extraction_status = "ocr" if text.strip() else "no_metadata"
    elif mime_type.endswith("wordprocessingml.document"):
        text = _extract_docx_text(path)
        extraction_status = "extracted" if text.strip() else "no_metadata"
    return _metadata_from_text(text), extraction_status


def _extract_pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages[:5])


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > 10 * 1024 * 1024:
            raise ValueError("DOCX document XML exceeds the extraction limit")
        content = archive.read(info)
    root = ElementTree.fromstring(content)
    return "\n".join(value for value in root.itertext() if value.strip())


def _metadata_from_text(text: str) -> dict[str, object]:
    result: dict[str, object] = {}
    amount_matches = re.findall(
        r"(?:gesamt|summe|total|betrag)[^\d]{0,20}([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    if amount_matches:
        with suppress(InvalidOperation):
            result["amount"] = Decimal(amount_matches[-1].replace(".", "").replace(",", "."))
    date_match = re.search(r"\b(\d{2}[./-]\d{2}[./-]\d{4})\b", text)
    if date_match:
        raw = date_match.group(1).replace("/", ".").replace("-", ".")
        with suppress(ValueError):
            result["document_date"] = datetime.strptime(raw, "%d.%m.%Y").date()
    reference = re.search(
        r"(?:rechnungs(?:nummer|nr\.?|#)|invoice\s*(?:no\.?|#))\s*[:#]?\s*([\w./-]+)",
        text,
        re.IGNORECASE,
    )
    if reference:
        result["reference"] = reference.group(1)
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line:
        result["merchant"] = first_line[:200]
    return result


def json_metadata(metadata: dict[str, object]) -> dict[str, str]:
    return {
        key: value.isoformat() if isinstance(value, date) else str(value)
        for key, value in metadata.items()
    }

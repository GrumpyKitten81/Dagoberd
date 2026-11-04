"""Local document persistence and explainable transaction matching."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from dagoberd.accounts import AccountScope
from dagoberd.ledger.models import to_minor_units
from dagoberd.ledger.repository import LedgerRepository, VersionConflictError


class DocumentMatcher:
    """Stores local invoices and ranks candidates without choosing ambiguity."""

    def __init__(
        self,
        repository: LedgerRepository,
        storage_dir: str | Path | None = None,
        *,
        auto_match_threshold: float = 0.95,
        auto_match_minimum_gap: float = 0.15,
        extract_pdf_text: bool = True,
    ) -> None:
        self.repository = repository
        self.storage_dir = Path(storage_dir) if storage_dir else None
        self.auto_match_threshold = auto_match_threshold
        self.auto_match_minimum_gap = auto_match_minimum_gap
        self.extract_pdf_text = extract_pdf_text

    def import_file(
        self,
        path: str | Path,
        *,
        amount: Decimal | None = None,
        document_date: date | None = None,
        merchant: str | None = None,
        reference: str | None = None,
        account_id: str | None = None,
        profile_code: str | None = None,
    ) -> dict:
        source = Path(path).resolve()
        if source.is_dir():
            files = sorted(
                item
                for item in source.rglob("*")
                if item.is_file() and item.suffix.casefold() in {".pdf"}
            )
            return {
                "directory": str(source),
                "documents": [
                    self.import_file(
                        item,
                        account_id=account_id,
                        profile_code=profile_code,
                    )
                    for item in files
                ],
            }
        if not source.is_file():
            raise FileNotFoundError(f"Document does not exist: {source}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ledger_documents WHERE content_hash=?", (digest,)
            ).fetchone()
            if existing:
                return {**dict(existing), "created": False}
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        extracted: dict[str, object] = {}
        extraction_status = "metadata"
        if mime_type == "application/pdf" and self.extract_pdf_text:
            extracted = self._extract_pdf(source)
            extraction_status = "extracted" if extracted else "no_metadata"
        extracted_amount = extracted.get("amount")
        extracted_date = extracted.get("document_date")
        extracted_merchant = extracted.get("merchant")
        extracted_reference = extracted.get("reference")
        if amount is None and isinstance(extracted_amount, Decimal):
            amount = extracted_amount
        if document_date is None and isinstance(extracted_date, date):
            document_date = extracted_date
        if merchant is None and isinstance(extracted_merchant, str):
            merchant = extracted_merchant
        if reference is None and isinstance(extracted_reference, str):
            reference = extracted_reference
        if reference:
            with self.repository.database.connect() as connection:
                duplicate = connection.execute(
                    """SELECT * FROM ledger_documents
                       WHERE reference=? AND COALESCE(merchant, '')=COALESCE(?, '')
                       ORDER BY created_at LIMIT 1""",
                    (str(reference), str(merchant) if merchant else None),
                ).fetchone()
                if duplicate:
                    return {**dict(duplicate), "created": False, "duplicate_by": "reference"}
        storage_path: Path | None = None
        if self.storage_dir:
            destination = self.storage_dir / account_id if account_id else self.storage_dir
            destination.mkdir(parents=True, exist_ok=True)
            storage_path = destination / f"{digest}{source.suffix.lower()}"
            if not storage_path.exists():
                shutil.copy2(source, storage_path)
        identifier = self.add_document(
            source.name,
            amount if isinstance(amount, Decimal) else None,
            document_date if isinstance(document_date, date) else None,
            str(merchant) if merchant else None,
            str(reference) if reference else None,
            storage_path=storage_path,
            content_hash=digest,
            mime_type=mime_type,
            extraction_status=extraction_status,
            metadata={
                key: (value.isoformat() if isinstance(value, date) else str(value))
                for key, value in extracted.items()
            }
            | {"account_id": account_id, "profile_code": profile_code},
        )
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_documents WHERE id=?", (identifier,)
            ).fetchone()
        return {**dict(row), "created": True}

    def add_document(
        self,
        filename: str,
        amount: Decimal | None,
        document_date: date | None = None,
        merchant: str | None = None,
        reference: str | None = None,
        *,
        storage_path: str | Path | None = None,
        content_hash: str | None = None,
        mime_type: str | None = None,
        extraction_status: str = "metadata",
        metadata: dict | None = None,
    ) -> str:
        identifier = f"document-{uuid4()}"
        with self.repository.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_documents(
                       id, filename, document_date, amount_minor, merchant,
                       reference, storage_path, content_hash, mime_type,
                       extraction_status, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    filename,
                    document_date.isoformat() if document_date else None,
                    to_minor_units(amount) if amount is not None else None,
                    merchant,
                    reference,
                    str(storage_path) if storage_path else None,
                    content_hash,
                    mime_type,
                    extraction_status,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.repository._audit(
                connection,
                "document",
                identifier,
                "import",
                None,
                {
                    "filename": filename,
                    "content_hash": content_hash,
                    "extraction_status": extraction_status,
                },
            )
        return identifier

    def list_documents(self, *, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM ledger_documents"
        parameters: tuple[object, ...] = ()
        if status:
            query += " WHERE status=?"
            parameters = (status,)
        query += " ORDER BY created_at DESC, id DESC"
        with self.repository.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def get_document(self, document_id: str) -> dict:
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_documents WHERE id=?", (document_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown document {document_id}")
        return dict(row)

    def preview_matches(
        self,
        document_id: str,
        *,
        account_scope: AccountScope | None = None,
        profile_code: str | None = None,
        limit: int = 100,
    ) -> dict:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.repository.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM ledger_documents WHERE id=?", (document_id,)
            ).fetchone()
        if document is None:
            raise KeyError(f"Unknown document {document_id}")
        metadata = json.loads(document["metadata_json"] or "{}")
        if metadata.get("account_id") and account_scope is None:
            account_scope = AccountScope.selected([metadata["account_id"]])
        profile_code = profile_code or metadata.get("profile_code")
        ranked: list[tuple[dict, float, list[str]]] = []
        for transaction in self.repository.list_transactions(
            limit=None,
            profile_code=profile_code,
            account_scope=account_scope,
        ):
            score, reasons = self._score(document, transaction)
            if score > 0:
                ranked.append((transaction, score, reasons))
        ranked.sort(key=lambda item: (-item[1], item[0]["id"]))
        unique_auto_match = bool(
            ranked
            and ranked[0][1] >= self.auto_match_threshold
            and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= self.auto_match_minimum_gap)
        )
        candidates = tuple(
            {
                "transaction_id": transaction["id"],
                "transaction_version": int(transaction["version"]),
                "score": score,
                "reasons": reasons,
                "would_auto_match": index == 0 and unique_auto_match,
            }
            for index, (transaction, score, reasons) in enumerate(ranked[:limit])
        )
        payload = {
            "document_id": document_id,
            "document_version": int(document["version"]),
            "profile_code": profile_code,
            "candidates": candidates,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return {**payload, "fingerprint": fingerprint, "truncated": len(ranked) > limit}

    def apply_match_preview(
        self,
        document_id: str,
        fingerprint: str,
        transaction_ids: list[str],
        *,
        expected_document_version: int,
        append: bool = True,
        account_scope: AccountScope | None = None,
        profile_code: str | None = None,
        limit: int = 100,
    ) -> dict:
        preview = self.preview_matches(
            document_id,
            account_scope=account_scope,
            profile_code=profile_code,
            limit=limit,
        )
        if preview["fingerprint"] != fingerprint:
            raise VersionConflictError("Document match preview changed; create a new preview")
        if preview["document_version"] != expected_document_version:
            raise VersionConflictError("Document changed; create a new preview")
        candidates = {item["transaction_id"]: item for item in preview["candidates"]}
        if not transaction_ids or any(
            identifier not in candidates for identifier in transaction_ids
        ):
            raise ValueError("Every selected transaction must be present in the match preview")
        results: list[dict] = []
        for transaction_id in transaction_ids:
            candidate = candidates[transaction_id]
            results.append(
                self.assign(
                    document_id,
                    transaction_id,
                    expected_document_version=(
                        expected_document_version if not results else expected_document_version + 1
                    ),
                    expected_transaction_version=candidate["transaction_version"],
                    append=append,
                )
            )
        return {
            "document_id": document_id,
            "fingerprint": fingerprint,
            "matches": results,
            "append": append,
        }

    def suggest(
        self,
        document_id: str,
        *,
        account_scope: AccountScope | None = None,
        profile_code: str | None = None,
    ) -> list[dict]:
        with self.repository.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM ledger_documents WHERE id=?", (document_id,)
            ).fetchone()
            if not document:
                raise KeyError(f"Unknown document {document_id}")
            metadata = json.loads(document["metadata_json"] or "{}")
            if metadata.get("account_id") and account_scope is None:
                account_scope = AccountScope.selected([metadata["account_id"]])
            profile_code = profile_code or metadata.get("profile_code")
            candidates = self.repository.list_transactions(
                limit=None,
                profile_code=profile_code,
                account_scope=account_scope,
            )
            ranked = []
            for transaction in candidates:
                score, reasons = self._score(document, transaction)
                if score > 0:
                    ranked.append((transaction, score, reasons))
            ranked.sort(key=lambda item: item[1], reverse=True)
            unique_auto_match = bool(
                ranked
                and ranked[0][1] >= self.auto_match_threshold
                and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= self.auto_match_minimum_gap)
            )
            matches: list[dict] = []
            for index, (transaction, score, reasons) in enumerate(ranked):
                status = "matched" if index == 0 and unique_auto_match else "suggested"
                match_id = f"document-match-{uuid4()}"
                connection.execute(
                    """INSERT INTO ledger_document_matches(
                           id, document_id, transaction_id, score,
                           reasons_json, status
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(document_id, transaction_id) DO UPDATE SET
                           score=excluded.score,
                           reasons_json=excluded.reasons_json,
                           status=CASE
                               WHEN ledger_document_matches.status='rejected'
                               THEN 'rejected' ELSE excluded.status END,
                           version=ledger_document_matches.version+1,
                           updated_at=CURRENT_TIMESTAMP""",
                    (
                        match_id,
                        document_id,
                        transaction["id"],
                        score,
                        json.dumps(reasons, ensure_ascii=False),
                        status,
                    ),
                )
                row = connection.execute(
                    """SELECT id, version, status FROM ledger_document_matches
                       WHERE document_id=? AND transaction_id=?""",
                    (document_id, transaction["id"]),
                ).fetchone()
                matches.append(
                    {
                        "match_id": row["id"],
                        "transaction_id": transaction["id"],
                        "score": score,
                        "status": row["status"],
                        "version": row["version"],
                        "reasons": reasons,
                    }
                )
            if unique_auto_match:
                connection.execute(
                    """UPDATE ledger_documents SET status='matched',
                       version=version+1 WHERE id=?""",
                    (document_id,),
                )
            return matches

    def review(self, match_id: str, decision: str, expected_version: int) -> dict:
        if decision not in {"accept", "reject"}:
            raise ValueError("Decision must be accept or reject")
        with self.repository.database.connect() as connection:
            match = connection.execute(
                "SELECT * FROM ledger_document_matches WHERE id=?", (match_id,)
            ).fetchone()
            if match is None:
                raise KeyError(f"Unknown document match {match_id}")
            if match["version"] != expected_version:
                raise VersionConflictError("Document match changed; reload it")
            status = "matched" if decision == "accept" else "rejected"
            if status == "matched":
                connection.execute(
                    """UPDATE ledger_document_matches SET status='rejected',
                       version=version+1, updated_at=CURRENT_TIMESTAMP
                       WHERE document_id=? AND id<>? AND status='matched'""",
                    (match["document_id"], match_id),
                )
            connection.execute(
                """UPDATE ledger_document_matches SET status=?, version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, match_id),
            )
            connection.execute(
                """UPDATE ledger_documents SET status=?, version=version+1
                   WHERE id=?""",
                ("matched" if status == "matched" else "open", match["document_id"]),
            )
            self.repository._audit(
                connection,
                "document_match",
                match_id,
                decision,
                {"status": match["status"], "version": expected_version},
                {
                    "status": status,
                    "version": expected_version + 1,
                    "score": match["score"],
                    "reasons": json.loads(match["reasons_json"]),
                },
            )
            return {"match_id": match_id, "status": status, "version": expected_version + 1}

    def assign(
        self,
        document_id: str,
        transaction_id: str,
        *,
        expected_document_version: int | None = None,
        expected_transaction_version: int | None = None,
        append: bool = False,
    ) -> dict:
        with self.repository.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM ledger_documents WHERE id=?", (document_id,)
            ).fetchone()
            if document is None:
                raise KeyError(f"Unknown document {document_id}")
            if (
                expected_document_version is not None
                and int(document["version"]) != expected_document_version
            ):
                raise VersionConflictError("Document changed; reload it before assigning")
            transaction = connection.execute(
                "SELECT id, version FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if transaction is None:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if (
                expected_transaction_version is not None
                and int(transaction["version"]) != expected_transaction_version
            ):
                raise VersionConflictError("Transaction changed; reload it before assigning")
            existing = connection.execute(
                "SELECT * FROM ledger_document_matches WHERE document_id=? AND transaction_id=?",
                (document_id, transaction_id),
            ).fetchone()
            if not append:
                connection.execute(
                    """UPDATE ledger_document_matches SET status='rejected', version=version+1,
                                   updated_at=CURRENT_TIMESTAMP WHERE document_id=? AND transaction_id<>? AND status='matched'""",
                    (document_id, transaction_id),
                )
            if existing:
                connection.execute(
                    """UPDATE ledger_document_matches SET score=1.0, status='matched', reasons_json='[\"manual\"]',
                                   version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (existing["id"],),
                )
                match_id = existing["id"]
            else:
                match_id = self.repository._id("document-match")
                connection.execute(
                    """INSERT INTO ledger_document_matches(id, document_id, transaction_id, score, reasons_json, status)
                                   VALUES (?, ?, ?, 1.0, '[\"manual\"]', 'matched')""",
                    (match_id, document_id, transaction_id),
                )
            connection.execute(
                "UPDATE ledger_documents SET status='matched', version=version+1 WHERE id=?",
                (document_id,),
            )
            connection.execute(
                """UPDATE ledger_review_items SET status='resolved', updated_at=CURRENT_TIMESTAMP
                               WHERE entity_type='document' AND entity_id=? AND status='open'""",
                (document_id,),
            )
            match = connection.execute(
                "SELECT * FROM ledger_document_matches WHERE id=?", (match_id,)
            ).fetchone()
            self.repository._audit(
                connection,
                "document_match",
                match_id,
                "document_assign",
                None,
                {
                    "document_id": document_id,
                    "transaction_id": transaction_id,
                    "score": 1.0,
                    "reasons": ["manual"],
                    "append": append,
                },
            )
            return {
                "match_id": match_id,
                "document_id": document_id,
                "transaction_id": transaction_id,
                "status": "matched",
                "version": match["version"],
            }

    def unmatch(self, match_id: str, expected_version: int) -> dict:
        with self.repository.database.connect() as connection:
            match = connection.execute(
                "SELECT * FROM ledger_document_matches WHERE id=?", (match_id,)
            ).fetchone()
            if match is None:
                raise KeyError(f"Unknown document match {match_id}")
            if match["version"] != expected_version:
                raise VersionConflictError("Document match changed; reload it")
            connection.execute(
                """UPDATE ledger_document_matches SET status='suggested',
                   version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (match_id,),
            )
            connection.execute(
                """UPDATE ledger_documents SET status='open', version=version+1
                   WHERE id=?""",
                (match["document_id"],),
            )
            self.repository._audit(
                connection,
                "document_match",
                match_id,
                "unmatch",
                {"status": match["status"]},
                {"status": "suggested"},
            )
        return {"match_id": match_id, "status": "suggested", "version": expected_version + 1}

    @staticmethod
    def _extract_pdf(path: Path) -> dict[str, object]:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages[:5])
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

    @staticmethod
    def _score(document: dict, transaction: dict) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        if document["amount_minor"] is not None and abs(document["amount_minor"]) == abs(
            transaction["amount_minor"]
        ):
            score += 0.65
            reasons.append("Amount matches")
        if (
            document["reference"]
            and document["reference"].casefold() in (transaction["reference"] or "").casefold()
        ):
            score += 0.25
            reasons.append("Reference matches")
        merchant = (document["merchant"] or "").casefold()
        searchable = " ".join(
            (transaction["purpose"] or "", transaction["counterparty"] or "")
        ).casefold()
        if merchant and merchant in searchable:
            score += 0.15
            reasons.append("Merchant in counterparty or purpose")
        if document["document_date"] and transaction["booking_date"]:
            delta = abs(
                (
                    date.fromisoformat(document["document_date"])
                    - date.fromisoformat(transaction["booking_date"])
                ).days
            )
            if delta <= 7:
                score += 0.1
                reasons.append("Date within seven days")
            elif delta <= 30:
                score += 0.03
                reasons.append("Date within 30 days")
        return min(score, 1.0), reasons

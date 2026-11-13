"""Two-step, idempotent file import shared by CLI and MCP."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from dagoberd.config import ImportAnnotationConfig
from dagoberd.importers import get_importer
from dagoberd.importers.base import FileImporter, ImportBatch, ImportIssue
from dagoberd.importers.tabular import TabularImporter
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.services.import_annotations import ImportAnnotationService


@dataclass(frozen=True, slots=True)
class ImportPreview:
    preview_id: str
    source: str
    account_id: str | None
    file_hash: str
    mapper_version: str
    count: int
    rows_total: int
    duplicate_candidates: int
    duplicates: tuple[dict, ...]
    duplicate_details_truncated: bool
    booking_from: str | None
    booking_to: str | None
    totals: dict[str, str]
    warnings: tuple[str, ...]
    error_count: int
    errors: tuple[dict, ...]
    errors_truncated: bool
    balance_observation_count: int = 0
    balance_snapshot_count: int = 0
    balance_snapshots: tuple[dict, ...] = ()
    account_actions: tuple[dict, ...] = ()
    transfer_candidate_count: int = 0
    transfer_pair_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImportExecution:
    preview_id: str
    import_run_id: str
    imported: int
    skipped: int
    accounts: int
    updated: int = 0
    annotations_applied: int = 0
    balance_snapshots: int = 0
    transfer_candidates: int = 0
    transfer_relations_suggested: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class ImportService:
    def __init__(
        self,
        repository: LedgerRepository,
        mappings: dict[str, dict[str, tuple[str, ...]]] | None = None,
        profiles: dict | None = None,
        max_file_size_mib: int = 50,
    ) -> None:
        self.repository = repository
        self.mappings = mappings or {}
        self.profiles = profiles or {}
        self.max_file_size = max_file_size_mib * 1024 * 1024

    def history(self, account_id: str, limit: int = 100) -> list[dict]:
        """Return account-scoped import runs without exposing file paths or source identities."""
        if not 1 <= limit <= 500:
            raise ValueError("History limit must be between 1 and 500")
        account = self.repository.get_account(account_id)
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.id, p.source, p.account_id, p.file_hash, p.mapper_version,
                          p.summary_json, p.executed_at, r.id AS run_id,
                          r.imported_count, r.skipped_count, r.updated_count, r.account_count
                   FROM ledger_import_previews p JOIN ledger_import_runs r ON r.preview_id=p.id
                   ORDER BY p.executed_at DESC, r.rowid DESC"""
            ).fetchall()
        results = []
        for row in rows:
            summary = json.loads(row["summary_json"])
            actions = summary.get("account_actions", [])
            matches = row["account_id"] == account_id or (
                row["account_id"] is None
                and any(
                    action.get("account_id") == account_id
                    or (
                        action.get("source_account") == account["external_id"]
                        and action.get("provider") == account["provider_name"]
                        and get_importer(row["source"]).provider_kind == account["provider_kind"]
                    )
                    for action in actions
                )
            )
            if not matches:
                continue
            results.append(
                {
                    "preview_id": row["id"],
                    "run_id": row["run_id"],
                    "source": row["source"],
                    "file_hash": row["file_hash"],
                    "mapper_version": row["mapper_version"],
                    "executed_at": row["executed_at"],
                    "booking_from": summary.get("booking_from"),
                    "booking_to": summary.get("booking_to"),
                    "imported": row["imported_count"],
                    "skipped": row["skipped_count"],
                    "updated": row["updated_count"],
                    "counts_scope": "entire_import" if row["account_id"] is None else "account",
                }
            )
            if len(results) == limit:
                break
        return results

    def preview(
        self,
        source: str,
        path: str | Path,
        account_id: str | None = None,
        profile: str | None = None,
    ) -> ImportPreview:
        importer = self._importer(source, profile)
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Import file does not exist: {file_path}")
        if file_path.stat().st_size > self.max_file_size:
            raise ValueError(
                f"Import file exceeds configured limit of {self.max_file_size // (1024 * 1024)} MiB"
            )
        account_external_id = self._account_external_id(
            importer.provider_kind,
            importer.requires_account,
            account_id,
        )
        provider_kind, provider_name = self._provider_context(importer, account_id)
        batch = importer.inspect_transactions(file_path, account_external_id)
        if not account_id:
            batch = self._validate_automatic_targets(batch, provider_kind, provider_name)
        transactions = list(batch.transactions)
        warnings: list[str] = []
        if batch.unidentified_account:
            warnings.append(
                "Source account identity is missing; confirm the selected account with the user"
            )
        annotation_config = self._annotation_config(profile)
        annotation_warnings: list[str] = []
        if annotation_config:
            plans = ImportAnnotationService(self.repository).plan(transactions, annotation_config)
            annotation_warnings = [warning for plan in plans for warning in plan.warnings]
            if annotation_warnings:
                warnings.append(f"{len(annotation_warnings)} Annotations-Warnung(en)")
        identities = [
            (transaction.account_external_id, transaction.external_id)
            for transaction in transactions
        ]
        repeated = sum(count - 1 for count in Counter(identities).values() if count > 1)
        if repeated:
            warnings.append(
                f"{repeated} repeated source identities in this file require review; "
                "references and content-derived identities may not distinguish identical transactions"
            )
        duplicates = self.repository.existing_source_identities(
            provider_kind,
            provider_name,
            identities,
        )
        dates = sorted(transaction.booking_date for transaction in transactions)
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for transaction in transactions:
            totals[transaction.currency] += transaction.amount
        if duplicates:
            warnings.append(
                f"{len(duplicates)} existing identities will be "
                "skipped or updated as status revisions"
            )
        if len(duplicates) > 100:
            warnings.append("Duplicate details are limited to 100 entries")
        if len(batch.issues) > 500:
            warnings.append("Error details are limited to 500 entries")
        if not transactions:
            warnings.append("The file contains no transactions")
        warnings.extend(self._balance_warnings(batch))
        if account_id:
            account_currency = self.repository.get_account(account_id)["currency"]
            transaction_currencies = {transaction.currency for transaction in transactions}
            if transaction_currencies - {account_currency}:
                warnings.append(
                    f"File currency does not match the configured account currency {account_currency}"
                )
        duplicate_summaries = tuple(
            {
                "identity_fingerprint": hashlib.sha256(
                    f"{account}|{external}".encode()
                ).hexdigest()[:16],
                "action": "skip_or_status_revision",
            }
            for account, external in sorted(duplicates)[:100]
        )
        error_summaries = tuple(issue.as_dict() for issue in batch.issues[:500])
        booking_from = dates[0].isoformat() if dates else None
        booking_to = dates[-1].isoformat() if dates else None
        totals_summary = {currency: str(amount) for currency, amount in sorted(totals.items())}
        account_types, account_display_names = self._account_metadata(profile)
        balance_snapshots = tuple(
            {
                "external_id_fingerprint": hashlib.sha256(item.external_id.encode()).hexdigest()[
                    :16
                ],
                "account_external_id": item.account_external_id,
                "balance_date": item.balance_date.isoformat(),
                "balance": str(item.balance),
                "currency": item.currency,
                "roles": item.roles,
                "source_row": item.source_row,
            }
            for item in batch.balance_snapshots[:500]
        )
        account_actions = self._account_actions(
            importer,
            provider_kind,
            provider_name,
            transactions,
            account_id,
            account_types,
            account_display_names,
        )
        transfer_candidate_count, transfer_pair_count = self._transfer_preview(transactions)
        summary: dict[str, Any] = {
            "profile": profile,
            "count": len(transactions),
            "rows_total": batch.rows_total,
            "duplicate_candidates": len(duplicates),
            "duplicates": duplicate_summaries,
            "duplicate_details_truncated": len(duplicates) > 100,
            "booking_from": booking_from,
            "booking_to": booking_to,
            "totals": totals_summary,
            "warnings": warnings,
            "annotation_warnings": annotation_warnings[:100],
            "annotation_warnings_truncated": len(annotation_warnings) > 100,
            "error_count": len(batch.issues),
            "errors": error_summaries,
            "errors_truncated": len(batch.issues) > 500,
            "balance_observation_count": len(batch.balance_observations),
            "balance_snapshot_count": len(batch.balance_snapshots),
            "balance_snapshots": balance_snapshots,
            "balance_snapshots_truncated": len(batch.balance_snapshots) > 500,
            "account_actions": account_actions,
            "transfer_candidate_count": transfer_candidate_count,
            "transfer_pair_count": transfer_pair_count,
        }
        file_hash = self._file_hash(file_path)
        preview_id = self.repository.create_import_preview(
            source=source,
            file_path=str(file_path),
            file_hash=file_hash,
            account_id=account_id,
            mapper_version=importer.version,
            summary=summary,
        )
        return ImportPreview(
            preview_id=preview_id,
            source=source,
            account_id=account_id,
            file_hash=file_hash,
            mapper_version=importer.version,
            count=len(transactions),
            rows_total=batch.rows_total,
            duplicate_candidates=len(duplicates),
            duplicates=duplicate_summaries,
            duplicate_details_truncated=len(duplicates) > 100,
            booking_from=booking_from,
            booking_to=booking_to,
            totals=totals_summary,
            warnings=tuple(warnings),
            error_count=len(batch.issues),
            errors=error_summaries,
            errors_truncated=len(batch.issues) > 500,
            balance_observation_count=len(batch.balance_observations),
            balance_snapshot_count=len(batch.balance_snapshots),
            balance_snapshots=balance_snapshots,
            account_actions=account_actions,
            transfer_candidate_count=transfer_candidate_count,
            transfer_pair_count=transfer_pair_count,
        )

    def execute(self, preview_id: str) -> ImportExecution:
        preview = self.repository.get_import_preview(preview_id)
        if preview["status"] != "prepared":
            raise ValueError("Import preview has already been consumed")
        if preview["summary"].get("errors"):
            raise ValueError(
                "Import preview contains validation errors; fix the file or mapping profile"
            )
        file_path = Path(preview["file_path"])
        try:
            if self._file_hash(file_path) != preview["file_hash"]:
                raise ValueError("Import file changed after preview; create a new preview")
            importer = self._importer(preview["source"], preview["summary"].get("profile"))
            if importer.version != preview["mapper_version"]:
                raise ValueError("Import mapping changed after preview; create a new preview")
            account_id = preview["account_id"]
            provider_kind, provider_name = self._provider_context(importer, account_id)
            account_external_id = self._account_external_id(
                importer.provider_kind,
                importer.requires_account,
                account_id,
            )
            batch = importer.inspect_transactions(file_path, account_external_id)
            if batch.issues:
                raise ValueError("Import file no longer passes validation; create a new preview")
            transactions = list(batch.transactions)
            profile = preview["summary"].get("profile")
            annotation_config = self._annotation_config(profile)
            annotation_service = ImportAnnotationService(self.repository)
            plans = (
                annotation_service.plan(transactions, annotation_config)
                if annotation_config
                else []
            )
            result = self.repository.import_transactions(
                provider_kind,
                provider_name,
                transactions,
                balance_snapshots=batch.balance_snapshots,
                category_annotations=annotation_service.category_annotations(plans),
                account_types=self._account_metadata(preview["summary"].get("profile"))[0],
                account_display_names=self._account_metadata(preview["summary"].get("profile"))[1],
            )
            annotations_applied = result.annotations_applied
            if annotation_config:
                apply_result = annotation_service.apply(
                    account_id,
                    plans,
                    provider_kind=provider_kind,
                    provider_name=provider_name,
                    include_categories=False,
                )
                annotations_applied += apply_result.applied
            run_id = self.repository.complete_import_preview(
                preview_id,
                result,
            )
        except Exception as error:
            self.repository.fail_import_preview(
                preview_id,
                error.__class__.__name__,
            )
            raise
        return ImportExecution(
            preview_id,
            run_id,
            result.imported,
            result.skipped,
            result.accounts,
            result.updated,
            annotations_applied,
            len(batch.balance_snapshots),
            result.transfer_candidates,
            result.transfer_relations_suggested,
        )

    @staticmethod
    def _transfer_preview(transactions: list) -> tuple[int, int]:
        candidates = [
            item
            for item in transactions
            if str(item.source_attributes.get("transaction_type", "")).casefold()
            == "externe umbuchung"
        ]
        unique_pairs: set[tuple[str, str]] = set()
        for candidate in candidates:
            matches = [
                item
                for item in transactions
                if item.external_id != candidate.external_id
                and item.account_external_id != candidate.account_external_id
                and item.currency == candidate.currency
                and item.amount == -candidate.amount
                and abs((item.booking_date - candidate.booking_date).days) <= 3
            ]
            if len(matches) == 1:
                unique_pairs.add(tuple(sorted((candidate.external_id, matches[0].external_id))))
        return len(candidates), len(unique_pairs)

    def _account_actions(
        self,
        importer: FileImporter,
        provider_kind: str,
        provider_name: str,
        transactions: list,
        account_id: str | None,
        account_types: dict[str, str],
        account_display_names: dict[str, str],
    ) -> tuple[dict, ...]:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for transaction in transactions:
            counts[(transaction.account_external_id, transaction.currency)] += 1
        if account_id:
            account = self.repository.get_account(account_id)
            return (
                {
                    "source_account": next(iter(counts), (account["external_id"], "EUR"))[0],
                    "currency": account["currency"],
                    "count": sum(counts.values()),
                    "action": "existing",
                    "account_id": account_id,
                },
            )
        actions: list[dict] = []
        with self.repository.database.connect() as connection:
            for (external_id, currency), count in sorted(counts.items()):
                row = connection.execute(
                    """SELECT a.id FROM ledger_accounts a
                       JOIN ledger_providers p ON p.id=a.provider_id
                       WHERE p.kind=? AND p.display_name=? AND a.external_id=?""",
                    (provider_kind, provider_name, external_id),
                ).fetchone()
                actions.append(
                    {
                        "source_account": external_id,
                        "currency": currency,
                        "count": count,
                        "action": "existing" if row else "create",
                        "account_id": row["id"] if row else None,
                        "provider": importer.provider_name,
                        "display_name": account_display_names.get(external_id, external_id),
                        "account_type": account_types.get(external_id, "checking"),
                    }
                )
        return tuple(actions)

    def _validate_automatic_targets(
        self, batch: ImportBatch, provider_kind: str, provider_name: str
    ) -> ImportBatch:
        issues = list(batch.issues)
        identities = {item.account_external_id for item in batch.transactions} | {
            item.account_external_id for item in batch.balance_snapshots
        }
        with self.repository.database.connect() as connection:
            for external_id in sorted(identities):
                row = connection.execute(
                    """SELECT a.id FROM ledger_accounts a
                       JOIN ledger_providers p ON p.id=a.provider_id
                       WHERE p.kind=? AND p.display_name=? AND a.external_id=?""",
                    (provider_kind, provider_name, external_id),
                ).fetchone()
                try:
                    if row:
                        target = self.repository._routed_account_id(connection, row["id"])
                        self.repository.require_bookable_account(connection, target)
                    else:
                        self.repository.require_no_disabled_identity(
                            connection, provider_kind, external_id
                        )
                except ValueError as error:
                    issues.append(ImportIssue(None, "account", "account_disabled", str(error)))
        return replace(batch, issues=tuple(issues))

    def _account_metadata(self, profile: str | None) -> tuple[dict[str, str], dict[str, str]]:
        if not profile or profile not in self.profiles:
            return {}, {}
        configured = self.profiles[profile]
        data = configured.model_dump() if hasattr(configured, "model_dump") else dict(configured)
        return (
            {str(key): str(value) for key, value in data.get("account_types", {}).items()},
            {str(key): str(value) for key, value in data.get("account_display_names", {}).items()},
        )

    @staticmethod
    def _balance_warnings(batch: Any) -> list[str]:
        by_account: dict[tuple[str, str], list] = defaultdict(list)
        transactions = {
            (item.account_external_id, item.external_id): item for item in batch.transactions
        }
        for observation in batch.balance_observations:
            by_account[(observation.account_external_id, observation.currency)].append(observation)
        warnings: list[str] = []
        for (account, currency), observations in sorted(by_account.items()):
            ordered = sorted(observations, key=lambda item: (item.balance_date, item.source_row))
            mismatches = 0
            for previous, current in pairwise(ordered):
                transaction = transactions.get((account, current.external_id))
                if transaction is None:
                    continue
                if current.balance - previous.balance != transaction.amount:
                    mismatches += 1
            if mismatches:
                warnings.append(
                    f"{account}: {mismatches} balance transitions in {currency} "
                    "cannot be reconciled because of source gaps"
                )
        return warnings

    def _account_external_id(
        self,
        provider_kind: str,
        required: bool,
        account_id: str | None,
    ) -> str | None:
        if not account_id:
            if required:
                raise ValueError("This import source requires a configured account ID")
            return None
        account = self.repository.get_account(account_id)
        if account["provider_kind"] != provider_kind and not (
            provider_kind == "file" and account["provider_kind"] == "manual"
        ):
            raise ValueError(
                f"Account {account_id} belongs to provider "
                f"{account['provider_kind']}, expected {provider_kind}"
            )
        if not account["enabled"]:
            raise ValueError(f"Account {account_id} is disabled")
        return account["external_id"]

    def _provider_context(
        self,
        importer: FileImporter,
        account_id: str | None,
    ) -> tuple[str, str]:
        if account_id:
            account = self.repository.get_account(account_id)
            if account["provider_kind"] == "manual":
                return "manual", account["provider_name"]
        return importer.provider_kind, self._provider_name(importer, account_id)

    def _annotation_config(self, profile: str | None) -> ImportAnnotationConfig | None:
        if not profile:
            return None
        try:
            configured = self.profiles[profile]
        except KeyError:
            return None
        raw = getattr(configured, "annotations", None)
        if raw is None:
            return None
        if isinstance(raw, ImportAnnotationConfig):
            return raw
        return ImportAnnotationConfig.model_validate(raw)

    def _provider_name(self, importer: FileImporter, account_id: str | None) -> str:
        if not account_id:
            return importer.provider_name
        return self.repository.get_account(account_id)["provider_name"]

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _importer(self, source: str, profile: str | None = None) -> FileImporter:
        if profile:
            try:
                configured = self.profiles[profile]
            except KeyError as error:
                raise ValueError(f"Unknown import profile: {profile}") from error
            data = (
                configured.model_dump() if hasattr(configured, "model_dump") else dict(configured)
            )
            configured_source = data.pop("source")
            if source != configured_source:
                raise ValueError(
                    f"Import profile {profile} uses source {configured_source}, not {source}"
                )
            mapping = data.pop("mapping", {})
            annotations = data.pop("annotations", None)
            data.pop("account_types", None)
            data.pop("account_display_names", None)
            skip_prefixes = ()
            if annotations:
                skip_prefixes = tuple(annotations.get("skip_purpose_prefixes", ()))
            if source == "tabular":
                return TabularImporter(
                    mapping,
                    skip_purpose_prefixes=skip_prefixes,
                    **{key: value for key, value in data.items() if key != "annotations"},
                )
            if source == "wiso365":
                return get_importer(source)
        mapping = self.mappings.get(source)
        if source == "tabular" and mapping:
            return TabularImporter(mapping)
        return get_importer(source)

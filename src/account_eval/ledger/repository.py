"""Persistence operations with optimistic writes and audit records."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

from account_eval.accounts import AccountScope

from .database import LedgerDatabase
from .models import (
    VALID_TRANSACTION_STATUS_TRANSITIONS,
    AccountType,
    ImportBalanceSnapshot,
    ImportCategoryAnnotation,
    ImportResult,
    NormalizedTransaction,
    TransactionRelationStatus,
    TransactionRelationType,
    TransactionStatus,
    account_type,
    to_minor_units,
    transaction_status,
)


class VersionConflictError(ValueError):
    def __init__(self, message: str, *, transaction_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.transaction_ids = transaction_ids


class SourceTransactionConflictError(ValueError):
    """A provider identity was reused for different immutable source data."""


_UNSET = object()


class LedgerRepository:
    def __init__(self, database: LedgerDatabase) -> None:
        self.database = database
        self.database.initialize()

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid4()}"

    @staticmethod
    def _normalized_payload(transaction: NormalizedTransaction) -> dict:
        return {
            "external_id": transaction.external_id,
            "booking_date": transaction.booking_date.isoformat(),
            "valuta_date": (
                transaction.valuta_date.isoformat() if transaction.valuta_date else None
            ),
            "amount_minor": to_minor_units(transaction.amount),
            "currency": transaction.currency.upper(),
            "purpose": transaction.purpose,
            "counterparty": transaction.counterparty,
            "reference": transaction.reference,
            "note": transaction.note,
            "source_attributes": transaction.source_attributes,
            "observed_balance": (
                str(transaction.observed_balance)
                if transaction.observed_balance is not None
                else None
            ),
            "row_kind": str(transaction.row_kind),
            "booking_status": transaction_status(transaction.booking_status).value,
        }

    @staticmethod
    def _revision_key(
        provider_id: str,
        account_external_id: str,
        normalized_payload: str,
        raw_payload: str,
    ) -> str:
        return hashlib.sha256(
            (f"{provider_id}|{account_external_id}|{normalized_payload}|{raw_payload}").encode()
        ).hexdigest()

    @staticmethod
    def _merchant_id_for_text(
        connection: sqlite3.Connection,
        value: str | None,
    ) -> str | None:
        if not value:
            return None
        display = " ".join(value.strip().split())
        normalized = display.casefold()
        row = connection.execute(
            """SELECT merchant_id FROM ledger_merchant_aliases
               WHERE normalized_alias=?""",
            (normalized,),
        ).fetchone()
        if row:
            return row["merchant_id"]
        row = connection.execute(
            """SELECT id FROM ledger_merchants
               WHERE canonical_name=? COLLATE NOCASE""",
            (display,),
        ).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _enqueue_review_item(
        connection: sqlite3.Connection,
        item_type: str,
        entity_type: str,
        entity_id: str,
        reason: str,
        priority: int,
    ) -> None:
        identifier = (
            "review-"
            + hashlib.sha256(f"{item_type}|{entity_type}|{entity_id}".encode()).hexdigest()[:32]
        )
        connection.execute(
            """INSERT OR IGNORE INTO ledger_review_items(
                   id, item_type, entity_type, entity_id, reason, priority
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (identifier, item_type, entity_type, entity_id, reason, priority),
        )

    def ensure_provider(self, kind: str, display_name: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM ledger_providers WHERE kind = ? AND display_name = ?",
                (kind, display_name),
            ).fetchone()
            if row:
                return row["id"]
            identifier = self._id("provider")
            connection.execute(
                "INSERT INTO ledger_providers(id, kind, display_name) VALUES (?, ?, ?)",
                (identifier, kind, display_name),
            )
            self._audit(
                connection,
                "provider",
                identifier,
                "create",
                None,
                {"kind": kind, "display_name": display_name},
            )
            return identifier

    def ensure_account(
        self,
        provider_id: str,
        external_id: str,
        display_name: str,
        currency: str,
        *,
        account_kind: AccountType | str | None = None,
    ) -> tuple[str, bool]:
        normalized_kind = account_type(account_kind).value if account_kind is not None else None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, account_type, version FROM ledger_accounts "
                "WHERE provider_id=? AND external_id=?",
                (provider_id, external_id),
            ).fetchone()
            if row:
                if normalized_kind is not None and row["account_type"] != normalized_kind:
                    connection.execute(
                        """UPDATE ledger_accounts
                           SET account_type=?, version=version+1, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (normalized_kind, row["id"]),
                    )
                    self._audit(
                        connection,
                        "account",
                        row["id"],
                        "update_type",
                        {"account_type": row["account_type"], "version": row["version"]},
                        {"account_type": normalized_kind, "version": row["version"] + 1},
                    )
                return row["id"], False
            identifier = self._id("account")
            connection.execute(
                """INSERT INTO ledger_accounts(
                       id, provider_id, external_id, display_name, currency, account_type
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    provider_id,
                    external_id,
                    display_name,
                    currency,
                    normalized_kind or AccountType.CHECKING.value,
                ),
            )
            self._audit(
                connection,
                "account",
                identifier,
                "create",
                None,
                {
                    "provider_id": provider_id,
                    "external_id": external_id,
                    "display_name": display_name,
                    "currency": currency,
                    "account_type": normalized_kind or AccountType.CHECKING.value,
                },
            )
            return identifier, True

    def routed_account_id(self, source_account_id: str) -> str:
        with self.database.connect() as connection:
            return self._routed_account_id(connection, source_account_id)

    @staticmethod
    def _routed_account_id(connection: sqlite3.Connection, source_account_id: str) -> str:
        route = connection.execute(
            """SELECT target_account_id FROM ledger_account_source_routes
               WHERE source_account_id=?""",
            (source_account_id,),
        ).fetchone()
        return str(route["target_account_id"]) if route else source_account_id

    def import_transactions(
        self,
        provider_kind: str,
        provider_name: str,
        transactions: Iterable[NormalizedTransaction],
        *,
        balance_snapshots: Iterable[ImportBalanceSnapshot] = (),
        category_annotations: Iterable[ImportCategoryAnnotation] = (),
        account_types: dict[str, AccountType | str] | None = None,
        account_display_names: dict[str, str] | None = None,
    ) -> ImportResult:
        transactions = tuple(transactions)
        balance_snapshots = tuple(balance_snapshots)
        category_annotations = tuple(category_annotations)
        imported = skipped = updated = accounts = 0
        balances_created = balances_skipped = 0
        account_cache: dict[str, str] = {}
        source_account_cache: dict[str, str] = {}
        transaction_cache: dict[tuple[str, str], str] = {}
        with self.database.connect() as connection:
            provider = connection.execute(
                "SELECT id FROM ledger_providers WHERE kind=? AND display_name=?",
                (provider_kind, provider_name),
            ).fetchone()
            if provider:
                provider_id = provider["id"]
            else:
                provider_id = self._id("provider")
                connection.execute(
                    "INSERT INTO ledger_providers(id, kind, display_name) VALUES (?, ?, ?)",
                    (provider_id, provider_kind, provider_name),
                )
                self._audit(
                    connection,
                    "provider",
                    provider_id,
                    "create",
                    None,
                    {"kind": provider_kind, "display_name": provider_name},
                )
            for transaction in transactions:
                source_account_id = source_account_cache.get(transaction.account_external_id)
                if source_account_id is None:
                    row = connection.execute(
                        "SELECT id FROM ledger_accounts WHERE provider_id=? AND external_id=?",
                        (provider_id, transaction.account_external_id),
                    ).fetchone()
                    if row:
                        source_account_id, created = row["id"], False
                    else:
                        source_account_id, created = self._id("account"), True
                        configured_type = account_type(
                            (account_types or {}).get(transaction.account_external_id, "checking")
                        ).value
                        display_name = (account_display_names or {}).get(
                            transaction.account_external_id, transaction.account_external_id
                        )
                        connection.execute(
                            """INSERT INTO ledger_accounts(
                                   id, provider_id, external_id, display_name,
                                   currency, account_type
                               ) VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                source_account_id,
                                provider_id,
                                transaction.account_external_id,
                                display_name,
                                transaction.currency,
                                configured_type,
                            ),
                        )
                        self._audit(
                            connection,
                            "account",
                            source_account_id,
                            "create",
                            None,
                            {
                                "provider_id": provider_id,
                                "external_id": transaction.account_external_id,
                                "currency": transaction.currency,
                                "display_name": display_name,
                                "account_type": configured_type,
                            },
                        )
                    source_account_cache[transaction.account_external_id] = source_account_id
                    accounts += int(created)
                account_id = self._routed_account_id(connection, source_account_id)
                account_cache[transaction.account_external_id] = account_id
                raw_payload = json.dumps(
                    transaction.raw or {},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                normalized = self._normalized_payload(transaction)
                normalized_payload = json.dumps(
                    normalized,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                revision_key = self._revision_key(
                    provider_id,
                    transaction.account_external_id,
                    normalized_payload,
                    raw_payload,
                )
                existing_identity = connection.execute(
                    """SELECT st.id, st.idempotency_key,
                              t.id AS transaction_id, t.booking_date,
                              t.valuta_date, t.amount_minor, t.currency,
                              t.purpose, t.counterparty, t.reference,
                              t.booking_status, t.version
                       FROM ledger_source_transactions st
                       JOIN ledger_transaction_sources link
                         ON link.source_transaction_id=st.id
                       JOIN ledger_transactions t ON t.id=link.transaction_id
                       WHERE st.provider_id=? AND st.account_id=?
                         AND st.external_id=?""",
                    (provider_id, source_account_id, transaction.external_id),
                ).fetchone()
                if existing_identity:
                    transaction_cache[
                        (transaction.account_external_id, transaction.external_id)
                    ] = str(existing_identity["transaction_id"])
                    if connection.execute(
                        """SELECT 1
                           FROM ledger_source_transaction_revisions
                           WHERE idempotency_key=?""",
                        (revision_key,),
                    ).fetchone():
                        skipped += 1
                        continue
                    immutable_fields = {
                        "amount_minor": existing_identity["amount_minor"],
                        "currency": existing_identity["currency"],
                    }
                    incoming_fields = {key: normalized[key] for key in immutable_fields}
                    if immutable_fields != incoming_fields:
                        raise SourceTransactionConflictError(
                            "Source transaction changed: immutable fields differ for "
                            "provider/account/external identity: "
                            f"{provider_kind}/"
                            f"{transaction.account_external_id}/"
                            f"{transaction.external_id}"
                        )
                    mutable_fields = {
                        "booking_date": existing_identity["booking_date"],
                        "valuta_date": existing_identity["valuta_date"],
                        "purpose": existing_identity["purpose"],
                        "counterparty": existing_identity["counterparty"],
                        "reference": existing_identity["reference"],
                    }
                    incoming_mutable_fields = {key: normalized[key] for key in mutable_fields}
                    mutable_changed = mutable_fields != incoming_mutable_fields
                    if (
                        mutable_changed
                        and existing_identity["booking_status"] != TransactionStatus.PENDING.value
                    ):
                        raise SourceTransactionConflictError(
                            "Source transaction changed: booked transaction fields "
                            "are immutable; use a correction identity"
                        )
                    connection.execute(
                        """INSERT INTO ledger_source_transaction_revisions(
                               id, source_transaction_id, idempotency_key,
                               raw_payload_json, normalized_payload_json,
                               booking_status
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            self._id("source-revision"),
                            existing_identity["id"],
                            revision_key,
                            raw_payload,
                            normalized_payload,
                            normalized["booking_status"],
                        ),
                    )
                    self._upsert_source_attributes(
                        connection,
                        existing_identity["id"],
                        transaction.source_attributes,
                    )
                    status_changed = (
                        existing_identity["booking_status"] != normalized["booking_status"]
                    )
                    next_version = int(existing_identity["version"])
                    if status_changed:
                        next_version = self._transition_transaction_status(
                            connection,
                            existing_identity["transaction_id"],
                            normalized["booking_status"],
                            int(existing_identity["version"]),
                            reason="Status aus Provider-/Importrevision",
                            source=provider_kind,
                        )
                    elif mutable_changed:
                        next_version += 1
                        connection.execute(
                            """UPDATE ledger_transactions
                               SET version=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (
                                next_version,
                                existing_identity["transaction_id"],
                            ),
                        )
                    if mutable_changed:
                        connection.execute(
                            """UPDATE ledger_transactions
                               SET booking_date=?, valuta_date=?, purpose=?,
                                   counterparty=?, reference=?,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE id=?""",
                            (
                                incoming_mutable_fields["booking_date"],
                                incoming_mutable_fields["valuta_date"],
                                incoming_mutable_fields["purpose"],
                                incoming_mutable_fields["counterparty"],
                                incoming_mutable_fields["reference"],
                                existing_identity["transaction_id"],
                            ),
                        )
                        self._audit(
                            connection,
                            "transaction",
                            existing_identity["transaction_id"],
                            "normalize_revision",
                            mutable_fields | {"version": existing_identity["version"]},
                            incoming_mutable_fields | {"version": next_version},
                            actor=provider_kind,
                        )
                    if status_changed or mutable_changed:
                        updated += 1
                    else:
                        skipped += 1
                    continue
                existing_key = connection.execute(
                    """SELECT source_transaction_id AS owner_id
                       FROM ledger_source_transaction_revisions
                       WHERE idempotency_key=?
                       UNION ALL
                       SELECT id AS owner_id
                       FROM ledger_source_transactions
                       WHERE idempotency_key=?
                       LIMIT 1""",
                    (revision_key, revision_key),
                ).fetchone()
                if existing_key:
                    raise SourceTransactionConflictError(
                        "Revision key already belongs to another source identity"
                    )
                source_id = self._id("source")
                transaction_id = self._id("transaction")
                connection.execute(
                    """INSERT INTO ledger_source_transactions
                       (id, provider_id, account_id, external_id, idempotency_key, raw_payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        provider_id,
                        source_account_id,
                        transaction.external_id,
                        revision_key,
                        raw_payload,
                    ),
                )
                self._upsert_source_attributes(
                    connection,
                    source_id,
                    transaction.source_attributes,
                )
                connection.execute(
                    """INSERT INTO ledger_source_transaction_revisions(
                           id, source_transaction_id, idempotency_key,
                           raw_payload_json, normalized_payload_json,
                           booking_status
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        self._id("source-revision"),
                        source_id,
                        revision_key,
                        raw_payload,
                        normalized_payload,
                        normalized["booking_status"],
                    ),
                )
                self._audit(
                    connection,
                    "source_transaction",
                    source_id,
                    "import",
                    None,
                    {
                        "provider_id": provider_id,
                        "account_id": source_account_id,
                        "external_id": transaction.external_id,
                    },
                    idempotency_key=f"source-import:{revision_key}",
                )
                merchant_id = self._merchant_id_for_text(
                    connection,
                    transaction.counterparty or transaction.purpose,
                )
                connection.execute(
                    """INSERT INTO ledger_transactions
                       (id, source_transaction_id, account_id, booking_date, valuta_date, amount_minor, currency,
                        purpose, counterparty, reference, note, merchant_id, booking_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        transaction_id,
                        source_id,
                        account_id,
                        transaction.booking_date.isoformat(),
                        transaction.valuta_date.isoformat() if transaction.valuta_date else None,
                        normalized["amount_minor"],
                        normalized["currency"],
                        transaction.purpose,
                        transaction.counterparty,
                        transaction.reference,
                        transaction.note or "",
                        merchant_id,
                        normalized["booking_status"],
                    ),
                )
                connection.execute(
                    """INSERT INTO ledger_transaction_sources(
                           transaction_id, source_transaction_id, is_primary
                       ) VALUES (?, ?, 1)""",
                    (transaction_id, source_id),
                )
                transaction_cache[(transaction.account_external_id, transaction.external_id)] = (
                    transaction_id
                )
                connection.execute(
                    """INSERT INTO ledger_transaction_status_events(
                           id, transaction_id, from_status, to_status,
                           transaction_version, reason, source
                       ) VALUES (?, ?, NULL, ?, 1, ?, ?)""",
                    (
                        self._id("status-event"),
                        transaction_id,
                        normalized["booking_status"],
                        "Status bei Erstimport",
                        provider_kind,
                    ),
                )
                self._audit(
                    connection,
                    "transaction",
                    transaction_id,
                    "import",
                    None,
                    {
                        "source_transaction_id": source_id,
                        "account_id": account_id,
                        "external_id": transaction.external_id,
                    },
                    idempotency_key=f"transaction-import:{revision_key}",
                )
                self._enqueue_review_item(
                    connection,
                    "new_transaction",
                    "transaction",
                    transaction_id,
                    "Review newly imported transaction",
                    60,
                )
                if normalized["booking_status"] == TransactionStatus.PENDING.value:
                    self._enqueue_review_item(
                        connection,
                        "pending_transaction",
                        "transaction",
                        transaction_id,
                        "Pending transaction requires final review",
                        80,
                    )
                if transaction.amount == 0:
                    self._enqueue_review_item(
                        connection,
                        "import_warning",
                        "transaction",
                        transaction_id,
                        "Review zero transaction from source file",
                        50,
                    )
                imported += 1
            annotations_applied = self._apply_import_category_annotations(
                connection,
                category_annotations,
                transaction_cache,
                account_cache,
            )
            for snapshot in balance_snapshots:
                source_account_id = source_account_cache.get(snapshot.account_external_id)
                if source_account_id is None:
                    row = connection.execute(
                        "SELECT id FROM ledger_accounts WHERE provider_id=? AND external_id=?",
                        (provider_id, snapshot.account_external_id),
                    ).fetchone()
                    if row:
                        source_account_id = row["id"]
                    else:
                        source_account_id = self._id("account")
                        configured_type = account_type(
                            (account_types or {}).get(snapshot.account_external_id, "checking")
                        ).value
                        display_name = (account_display_names or {}).get(
                            snapshot.account_external_id, snapshot.account_external_id
                        )
                        connection.execute(
                            """INSERT INTO ledger_accounts(
                                   id, provider_id, external_id, display_name,
                                   currency, account_type
                               ) VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                source_account_id,
                                provider_id,
                                snapshot.account_external_id,
                                display_name,
                                snapshot.currency,
                                configured_type,
                            ),
                        )
                        accounts += 1
                    source_account_cache[snapshot.account_external_id] = source_account_id
                account_id = self._routed_account_id(connection, source_account_id)
                account_cache[snapshot.account_external_id] = account_id
                created = self._record_imported_balance_snapshot(
                    connection,
                    account_id,
                    snapshot,
                    source=provider_name,
                )
                balances_created += int(created)
                balances_skipped += int(not created)
            transfer_candidates, transfer_relations = self._apply_import_transfer_suggestions(
                connection,
                transactions,
                transaction_cache,
            )
        return ImportResult(
            imported=imported,
            skipped=skipped,
            accounts=accounts,
            updated=updated,
            balance_snapshots_created=balances_created,
            balance_snapshots_skipped=balances_skipped,
            annotations_applied=annotations_applied,
            transfer_candidates=transfer_candidates,
            transfer_relations_suggested=transfer_relations,
        )

    def _apply_import_transfer_suggestions(
        self,
        connection: sqlite3.Connection,
        transactions: tuple[NormalizedTransaction, ...],
        transaction_cache: dict[tuple[str, str], str],
    ) -> tuple[int, int]:
        candidates = [
            item
            for item in transactions
            if str(item.source_attributes.get("transaction_type", "")).casefold()
            == "externe umbuchung"
        ]
        proposals: dict[str, str] = {}
        for candidate in candidates:
            candidate_id = transaction_cache.get(
                (candidate.account_external_id, candidate.external_id)
            )
            if candidate_id is None:
                continue
            self._enqueue_review_item(
                connection,
                "relation_suggestion",
                "transaction",
                candidate_id,
                "Review WISO transfer candidate",
                65,
            )
            matches = [
                item
                for item in transactions
                if item.external_id != candidate.external_id
                and item.account_external_id != candidate.account_external_id
                and item.currency == candidate.currency
                and item.amount == -candidate.amount
                and candidate.amount != 0
                and abs((item.booking_date - candidate.booking_date).days) <= 3
            ]
            if len(matches) != 1:
                continue
            target = matches[0]
            target_id = transaction_cache.get((target.account_external_id, target.external_id))
            if target_id is not None:
                proposals[candidate_id] = target_id
        target_counts: dict[str, int] = {}
        for target_id in proposals.values():
            target_counts[target_id] = target_counts.get(target_id, 0) + 1
        suggested = 0
        seen_pairs: set[tuple[str, str]] = set()
        for candidate_id, target_id in proposals.items():
            if target_counts[target_id] != 1:
                continue
            pair = (
                (candidate_id, target_id) if candidate_id < target_id else (target_id, candidate_id)
            )
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            source = connection.execute(
                "SELECT amount_minor, version FROM ledger_transactions WHERE id=?",
                (candidate_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT amount_minor, version FROM ledger_transactions WHERE id=?",
                (target_id,),
            ).fetchone()
            if source is None or target is None:
                continue
            source_id, destination_id = (
                (candidate_id, target_id)
                if int(source["amount_minor"]) < 0
                else (target_id, candidate_id)
            )
            existing = connection.execute(
                """SELECT id FROM ledger_transaction_relations
                   WHERE relation_type='transfer' AND source_transaction_id=?
                     AND target_transaction_id=?""",
                (source_id, destination_id),
            ).fetchone()
            if existing:
                continue
            source_version = int(
                connection.execute(
                    "SELECT version FROM ledger_transactions WHERE id=?", (source_id,)
                ).fetchone()[0]
            )
            target_version = int(
                connection.execute(
                    "SELECT version FROM ledger_transactions WHERE id=?", (destination_id,)
                ).fetchone()[0]
            )
            relation_id = self._id("transaction-relation")
            connection.execute(
                """INSERT INTO ledger_transaction_relations(
                       id, relation_type, source_transaction_id, target_transaction_id,
                       status, score, reasons_json, source_version, target_version,
                       metadata_json
                   ) VALUES (?, 'transfer', ?, ?, 'suggested', 0.95, ?, ?, ?, ?)""",
                (
                    relation_id,
                    source_id,
                    destination_id,
                    json.dumps(
                        ["WISO-Art Externe Umbuchung", "Opposite amount and date are unambiguous"]
                    ),
                    source_version,
                    target_version,
                    json.dumps({"origin": "file_import", "activation": "review_required"}),
                ),
            )
            self._enqueue_review_item(
                connection,
                "relation_suggestion",
                "transaction_relation",
                relation_id,
                "Review suggested WISO transfer relation",
                70,
            )
            suggested += 1
        return len(candidates), suggested

    def _apply_import_category_annotations(
        self,
        connection: sqlite3.Connection,
        annotations: tuple[ImportCategoryAnnotation, ...],
        transaction_cache: dict[tuple[str, str], str],
        account_cache: dict[str, str],
    ) -> int:
        applied = 0
        for annotation in annotations:
            transaction_id = transaction_cache.get(
                (annotation.account_external_id, annotation.external_id)
            )
            account_id = account_cache.get(annotation.account_external_id)
            if transaction_id is None or account_id is None:
                continue
            profile = connection.execute(
                "SELECT id FROM ledger_profiles WHERE code=?",
                (annotation.profile_code,),
            ).fetchone()
            if profile:
                profile_id = str(profile["id"])
            else:
                profile_id = self._id("profile")
                connection.execute(
                    "INSERT INTO ledger_profiles(id, code, name) VALUES (?, ?, ?)",
                    (profile_id, annotation.profile_code, annotation.profile_code),
                )
                self._audit(
                    connection,
                    "profile",
                    profile_id,
                    "create",
                    None,
                    {"code": annotation.profile_code, "name": annotation.profile_code},
                )
            category = connection.execute(
                "SELECT id FROM ledger_categories WHERE code=?",
                (annotation.category_code,),
            ).fetchone()
            if category:
                category_id = str(category["id"])
            else:
                category_id = self._id("category")
                connection.execute(
                    "INSERT INTO ledger_categories(id, code, name) VALUES (?, ?, ?)",
                    (category_id, annotation.category_code, annotation.category_name),
                )
                self._audit(
                    connection,
                    "category",
                    category_id,
                    "create",
                    None,
                    {
                        "code": annotation.category_code,
                        "name": annotation.category_name,
                    },
                )
            connection.execute(
                """INSERT OR IGNORE INTO ledger_profile_categories(profile_id, category_id)
                   VALUES (?, ?)""",
                (profile_id, category_id),
            )
            position = connection.execute(
                "SELECT position FROM ledger_account_profiles WHERE account_id=? AND profile_id=?",
                (account_id, profile_id),
            ).fetchone()
            if position is None:
                next_position = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(position), -1) + 1
                           FROM ledger_account_profiles WHERE account_id=?""",
                        (account_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """INSERT INTO ledger_account_profiles(account_id, profile_id, position)
                       VALUES (?, ?, ?)""",
                    (account_id, profile_id, next_position),
                )
            existing = connection.execute(
                """SELECT 1 FROM ledger_classifications
                   WHERE transaction_id=? AND profile_id=? AND category_id=?
                     AND source='manual' AND rationale='Import annotation'
                     AND status='applied'""",
                (transaction_id, profile_id, category_id),
            ).fetchone()
            if existing:
                continue
            classification_id = self._id("classification")
            connection.execute(
                """INSERT INTO ledger_classifications(
                       id, transaction_id, profile_id, category_id, source,
                       confidence, rationale, status, metadata_json
                   ) VALUES (?, ?, ?, ?, 'manual', 1.0, 'Import annotation',
                             'applied', '{"origin":"file_import"}')""",
                (classification_id, transaction_id, profile_id, category_id),
            )
            connection.execute(
                """UPDATE ledger_transactions
                   SET version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "classify",
                None,
                {
                    "profile": annotation.profile_code,
                    "category": annotation.category_code,
                    "source": "import",
                },
            )
            applied += 1
        return applied

    def attach_source_alias(
        self,
        provider_kind: str,
        provider_name: str,
        transaction: NormalizedTransaction,
        target_transaction_id: str,
        *,
        expected_transaction_version: int,
    ) -> dict:
        """Attach an additional immutable source identity to a canonical transaction."""
        raw_payload = json.dumps(
            transaction.raw or {}, sort_keys=True, separators=(",", ":"), default=str
        )
        normalized = self._normalized_payload(transaction)
        normalized_payload = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), default=str
        )
        with self.database.connect() as connection:
            target = connection.execute(
                """SELECT id, version, amount_minor, currency
                   FROM ledger_transactions WHERE id=?""",
                (target_transaction_id,),
            ).fetchone()
            if target is None:
                raise KeyError(f"Unknown transaction {target_transaction_id}")
            if int(target["version"]) != expected_transaction_version:
                raise VersionConflictError("The transaction changed; recreate the alias preview.")
            if (
                int(target["amount_minor"]) != int(normalized["amount_minor"])
                or target["currency"] != normalized["currency"]
            ):
                raise SourceTransactionConflictError(
                    "Alias source amount/currency differs from the target transaction"
                )
            provider = connection.execute(
                "SELECT id FROM ledger_providers WHERE kind=? AND display_name=?",
                (provider_kind, provider_name),
            ).fetchone()
            if provider:
                provider_id = provider["id"]
            else:
                provider_id = self._id("provider")
                connection.execute(
                    "INSERT INTO ledger_providers(id, kind, display_name) VALUES (?, ?, ?)",
                    (provider_id, provider_kind, provider_name),
                )
            account = connection.execute(
                "SELECT id FROM ledger_accounts WHERE provider_id=? AND external_id=?",
                (provider_id, transaction.account_external_id),
            ).fetchone()
            if account:
                account_id = account["id"]
            else:
                account_id = self._id("account")
                connection.execute(
                    """INSERT INTO ledger_accounts(
                           id, provider_id, external_id, display_name, currency
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        provider_id,
                        transaction.account_external_id,
                        transaction.account_external_id,
                        transaction.currency,
                    ),
                )
            existing = connection.execute(
                """SELECT st.id, link.transaction_id
                   FROM ledger_source_transactions st
                   LEFT JOIN ledger_transaction_sources link
                     ON link.source_transaction_id=st.id
                   WHERE st.provider_id=? AND st.account_id=? AND st.external_id=?""",
                (provider_id, account_id, transaction.external_id),
            ).fetchone()
            if existing:
                if existing["transaction_id"] != target_transaction_id:
                    raise SourceTransactionConflictError(
                        "Source identity is already linked to another transaction"
                    )
                return {
                    "transaction_id": target_transaction_id,
                    "source_transaction_id": existing["id"],
                    "created": False,
                }
            revision_key = self._revision_key(
                provider_id,
                transaction.account_external_id,
                normalized_payload,
                raw_payload,
            )
            source_id = self._id("source")
            connection.execute(
                """INSERT INTO ledger_source_transactions(
                       id, provider_id, account_id, external_id,
                       idempotency_key, raw_payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    provider_id,
                    account_id,
                    transaction.external_id,
                    revision_key,
                    raw_payload,
                ),
            )
            connection.execute(
                """INSERT INTO ledger_source_transaction_revisions(
                       id, source_transaction_id, idempotency_key,
                       raw_payload_json, normalized_payload_json, booking_status
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._id("source-revision"),
                    source_id,
                    revision_key,
                    raw_payload,
                    normalized_payload,
                    normalized["booking_status"],
                ),
            )
            self._upsert_source_attributes(connection, source_id, transaction.source_attributes)
            connection.execute(
                """INSERT INTO ledger_transaction_sources(
                       transaction_id, source_transaction_id, is_primary
                   ) VALUES (?, ?, 0)""",
                (target_transaction_id, source_id),
            )
            self._audit(
                connection,
                "transaction",
                target_transaction_id,
                "attach_source_alias",
                None,
                {
                    "source_transaction_id": source_id,
                    "provider_kind": provider_kind,
                    "provider_name": provider_name,
                },
            )
            return {
                "transaction_id": target_transaction_id,
                "source_transaction_id": source_id,
                "created": True,
            }

    @staticmethod
    def _upsert_source_attributes(
        connection: sqlite3.Connection,
        source_transaction_id: str,
        attributes: dict[str, object],
    ) -> None:
        for key, value in sorted(attributes.items()):
            if value is None:
                continue
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            connection.execute(
                """INSERT INTO ledger_source_transaction_attributes(
                       source_transaction_id, attribute_key, attribute_value, value_json
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_transaction_id, attribute_key) DO UPDATE SET
                       attribute_value=excluded.attribute_value,
                       value_json=excluded.value_json,
                       updated_at=CURRENT_TIMESTAMP""",
                (source_transaction_id, key, str(value), serialized),
            )

    def _record_imported_balance_snapshot(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        snapshot: ImportBalanceSnapshot,
        *,
        source: str,
    ) -> bool:
        balance_minor = to_minor_units(snapshot.balance)
        external_id = f"{snapshot.external_id}:{','.join(snapshot.roles)}"
        idempotency_key = hashlib.sha256(
            (
                f"{account_id}|{snapshot.balance_date.isoformat()}|{balance_minor}|"
                f"{snapshot.currency}|{source}|{external_id}"
            ).encode()
        ).hexdigest()
        if connection.execute(
            "SELECT 1 FROM ledger_balance_snapshots WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone():
            return False
        account = connection.execute(
            "SELECT currency, last_balance_at FROM ledger_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise KeyError(f"Unknown account {account_id}")
        if account["currency"] != snapshot.currency:
            raise ValueError(
                f"Balance currency {snapshot.currency} does not match account currency "
                f"{account['currency']}"
            )
        identifier = self._id("balance-snapshot")
        metadata = {
            "roles": snapshot.roles,
            "source_row": snapshot.source_row,
            "source_attributes": snapshot.source_attributes,
        }
        connection.execute(
            """INSERT INTO ledger_balance_snapshots(
                   id, account_id, balance_date, balance_minor, currency,
                   source, external_id, idempotency_key, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                identifier,
                account_id,
                snapshot.balance_date.isoformat(),
                balance_minor,
                snapshot.currency,
                source,
                external_id,
                idempotency_key,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        current_date = str(account["last_balance_at"])[:10] if account["last_balance_at"] else None
        if current_date is None or snapshot.balance_date.isoformat() >= current_date:
            connection.execute(
                """UPDATE ledger_accounts SET last_balance_minor=?, last_balance_at=?,
                       version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (balance_minor, snapshot.balance_date.isoformat(), account_id),
            )
        self._audit(
            connection,
            "balance_snapshot",
            identifier,
            "import",
            None,
            {"account_id": account_id, "source": source, "external_id": external_id},
        )
        return True

    def list_transactions(
        self,
        limit: int | None = 100,
        booking_from: str | None = None,
        booking_to: str | None = None,
        profile_code: str | None = None,
        account_scope: AccountScope | None = None,
        booking_statuses: tuple[str, ...] | None = (
            TransactionStatus.PENDING.value,
            TransactionStatus.BOOKED.value,
        ),
    ) -> list[dict]:
        profile_filter = ""
        parameters: list[object] = []
        if profile_code:
            profile_filter = """AND c2.profile_id = (
                SELECT id FROM ledger_profiles WHERE code=?)"""
            parameters.append(profile_code)
        metadata = "COALESCE(sp.metadata_json, cl.metadata_json, '{}')"
        query = f"""SELECT t.*,
                   CASE
                     WHEN t.booking_status<>'booked' THEN 0
                     WHEN EXISTS (
                       SELECT 1 FROM ledger_transaction_relations relation
                       WHERE relation.status='confirmed'
                         AND relation.relation_type='transfer'
                         AND (relation.source_transaction_id=t.id
                              OR relation.target_transaction_id=t.id)
                     ) THEN COALESCE((
                       SELECT -relation.fee_amount_minor
                       FROM ledger_transaction_relations relation
                       WHERE relation.status='confirmed'
                         AND relation.relation_type='transfer'
                         AND relation.source_transaction_id=t.id
                       ORDER BY relation.updated_at DESC LIMIT 1
                     ), 0)
                     WHEN EXISTS (
                       SELECT 1 FROM ledger_transaction_relations relation
                       WHERE relation.status='confirmed'
                         AND relation.relation_type='reversal'
                         AND (relation.source_transaction_id=t.id
                              OR relation.target_transaction_id=t.id)
                     ) THEN 0
                     ELSE t.amount_minor
                   END AS cashflow_amount_minor,
                   (SELECT GROUP_CONCAT(DISTINCT relation.relation_type)
                    FROM ledger_transaction_relations relation
                    WHERE relation.status='confirmed'
                      AND (relation.source_transaction_id=t.id
                           OR relation.target_transaction_id=t.id))
                       AS confirmed_relation_types,
                   c.name AS category, c.code AS category_code,
                   merchant.canonical_name AS merchant,
                   COALESCE((SELECT json_group_array(name) FROM (
                       SELECT tag.name AS name
                       FROM ledger_transaction_tags tt
                       JOIN ledger_tags tag ON tag.id=tt.tag_id
                       WHERE tt.transaction_id=COALESCE(sp.transaction_id, t.id)
                       ORDER BY tag.name COLLATE NOCASE
                   )), '[]') AS tags_json,
                   COALESCE((SELECT json_group_object(code, value_code) FROM (
                       SELECT dimension.code AS code, value.code AS value_code
                       FROM ledger_transaction_dimensions td
                       JOIN ledger_dimensions dimension
                         ON dimension.id=td.dimension_id
                       JOIN ledger_dimension_values value
                         ON value.id=td.dimension_value_id
                       WHERE td.transaction_id=COALESCE(sp.transaction_id, t.id)
                       ORDER BY dimension.code
                   )), '{{}}') AS dimensions_json,
                   cl.confidence, cl.source AS classification_source,
                   sp.id AS split_id,
                   COALESCE(sp.memo, cl.rationale) AS comment,
                   json_extract({metadata}, '$.person_id') AS person_id,
                   COALESCE(d.external_id,
                            json_extract({metadata}, '$.invoice_number')) AS invoice_number,
                   d.storage_path AS document_path,
                   CASE WHEN dm.id IS NOT NULL THEN 'matched'
                        ELSE 'unmatched' END AS document_status,
                   COALESCE(json_extract(d.metadata_json, '$.invoice_type'),
                            json_extract({metadata}, '$.invoice_type')) AS invoice_type
                   FROM v_ledger_active_transactions t
                   LEFT JOIN ledger_classifications cl ON cl.id = (
                       SELECT id FROM ledger_classifications c2 WHERE c2.transaction_id=t.id
                       AND c2.status='applied' {profile_filter}
                       ORDER BY c2.created_at DESC, c2.id DESC LIMIT 1)
                   LEFT JOIN ledger_splits sp ON sp.id=t.id
                   LEFT JOIN ledger_categories c ON c.id=COALESCE(t.split_category_id, cl.category_id)
                   LEFT JOIN ledger_merchants merchant ON merchant.id=t.merchant_id
                   LEFT JOIN ledger_document_matches dm ON dm.id = (
                       SELECT id FROM ledger_document_matches dm2
                       WHERE dm2.transaction_id=t.id AND dm2.status='matched'
                       ORDER BY dm2.created_at DESC, dm2.id DESC LIMIT 1)
                   LEFT JOIN ledger_documents d ON d.id=dm.document_id
                   WHERE 1=1"""
        if booking_statuses is not None:
            normalized_statuses = tuple(
                transaction_status(status).value for status in booking_statuses
            )
            if not normalized_statuses:
                query += " AND 1=0"
            else:
                placeholders = ",".join("?" for _ in normalized_statuses)
                query += f" AND t.booking_status IN ({placeholders})"
                parameters.extend(normalized_statuses)
        if booking_from:
            query += " AND t.booking_date >= ?"
            parameters.append(booking_from)
        if booking_to:
            query += " AND t.booking_date <= ?"
            parameters.append(booking_to)
        if account_scope is not None:
            account_ids = self.resolve_account_scope(account_scope)
            if not account_ids:
                query += " AND 1=0"
            else:
                placeholders = ",".join("?" for _ in account_ids)
                query += f" AND t.account_id IN ({placeholders})"
                parameters.extend(account_ids)
        query += " ORDER BY t.booking_date DESC, t.id DESC"
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1")
            query += " LIMIT ?"
            parameters.append(min(limit, 500))
        with self.database.connect() as connection:
            records = [dict(row) for row in connection.execute(query, parameters)]
        for record in records:
            record["tags"] = json.loads(record.pop("tags_json"))
            record["dimensions"] = json.loads(record.pop("dimensions_json"))
        return records

    def transaction_status_counts(
        self,
        *,
        booking_from: str | None = None,
        booking_to: str | None = None,
        account_scope: AccountScope | None = None,
        currency: str | None = None,
    ) -> dict[str, int]:
        query = """SELECT t.booking_status, COUNT(*) AS count
                   FROM ledger_transactions t
                   WHERE t.state<>'void'"""
        parameters: list[object] = []
        if booking_from:
            query += " AND t.booking_date>=?"
            parameters.append(booking_from)
        if booking_to:
            query += " AND t.booking_date<=?"
            parameters.append(booking_to)
        if currency:
            query += " AND t.currency=?"
            parameters.append(currency.upper())
        scope = account_scope or AccountScope.all()
        account_ids = self.resolve_account_scope(scope)
        if not account_ids:
            return {}
        placeholders = ",".join("?" for _ in account_ids)
        query += f" AND t.account_id IN ({placeholders})"
        parameters.extend(account_ids)
        query += " GROUP BY t.booking_status ORDER BY t.booking_status"
        with self.database.connect() as connection:
            return {
                row["booking_status"]: int(row["count"])
                for row in connection.execute(query, parameters)
            }

    def get_transaction(self, transaction_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?",
                (transaction_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown transaction {transaction_id}")
        return dict(row)

    def transaction_detail(self, transaction_id: str) -> dict:
        record = self.get_transaction(transaction_id)
        with self.database.connect() as connection:
            origins = [
                dict(row)
                for row in connection.execute(
                    """SELECT st.id AS source_transaction_id, st.external_id,
                              p.kind AS provider_kind, p.display_name AS provider_name,
                              a.external_id AS account_external_id, link.is_primary,
                              st.imported_at
                       FROM ledger_transaction_sources link
                       JOIN ledger_source_transactions st
                         ON st.id=link.source_transaction_id
                       JOIN ledger_providers p ON p.id=st.provider_id
                       JOIN ledger_accounts a ON a.id=st.account_id
                       WHERE link.transaction_id=?
                       ORDER BY link.is_primary DESC, st.imported_at, st.id""",
                    (transaction_id,),
                )
            ]
            for origin in origins:
                origin["source_attributes"] = {
                    row["attribute_key"]: json.loads(row["value_json"])
                    for row in connection.execute(
                        """SELECT attribute_key, value_json
                           FROM ledger_source_transaction_attributes
                           WHERE source_transaction_id=? ORDER BY attribute_key""",
                        (origin["source_transaction_id"],),
                    )
                }
            record["source_origins"] = origins
            record["source_attributes"] = {
                key: value
                for origin in origins
                for key, value in origin["source_attributes"].items()
            }
            record["tags"] = [
                row["name"]
                for row in connection.execute(
                    """SELECT tag.name FROM ledger_transaction_tags tt
                       JOIN ledger_tags tag ON tag.id=tt.tag_id
                       WHERE tt.transaction_id=? ORDER BY tag.name COLLATE NOCASE""",
                    (transaction_id,),
                )
            ]
            record["dimensions"] = {
                row["dimension_code"]: row["value_code"]
                for row in connection.execute(
                    """SELECT d.code AS dimension_code, dv.code AS value_code
                       FROM ledger_transaction_dimensions td
                       JOIN ledger_dimensions d ON d.id=td.dimension_id
                       JOIN ledger_dimension_values dv ON dv.id=td.dimension_value_id
                       WHERE td.transaction_id=? ORDER BY d.code""",
                    (transaction_id,),
                )
            }
            record["documents"] = [
                dict(row)
                for row in connection.execute(
                    """SELECT d.*, dm.id AS match_id, dm.status AS match_status, dm.score
                       FROM ledger_document_matches dm
                       JOIN ledger_documents d ON d.id=dm.document_id
                       WHERE dm.transaction_id=? ORDER BY dm.score DESC""",
                    (transaction_id,),
                )
            ]
            record["splits"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ledger_splits WHERE transaction_id=? ORDER BY created_at, id",
                    (transaction_id,),
                )
            ]
        record["relations"] = self.list_transaction_relations(transaction_id=transaction_id)
        record["status_history"] = self.list_transaction_status_history(transaction_id)
        record["classifications"] = self.list_classifications(transaction_id=transaction_id)
        return record

    def _transition_transaction_status(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        new_status: str | TransactionStatus,
        expected_version: int,
        *,
        reason: str,
        source: str,
        allow_terminal_reset: bool = False,
    ) -> int:
        target = transaction_status(new_status)
        row = connection.execute(
            """SELECT booking_status, state, version
               FROM ledger_transactions WHERE id=?""",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown transaction {transaction_id}")
        if int(row["version"]) != expected_version:
            raise VersionConflictError("The transaction has changed; reload it before writing.")
        current = transaction_status(row["booking_status"])
        if current == target:
            return expected_version
        if row["state"] == "split_parent":
            raise ValueError("Revert the split before changing the booking status.")
        if not allow_terminal_reset and target not in VALID_TRANSACTION_STATUS_TRANSITIONS[current]:
            raise ValueError(
                f"Invalid transaction status transition: {current.value} -> {target.value}"
            )
        next_version = expected_version + 1
        connection.execute(
            """UPDATE ledger_transactions
               SET booking_status=?, version=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (target.value, next_version, transaction_id),
        )
        connection.execute(
            """INSERT INTO ledger_transaction_status_events(
                   id, transaction_id, from_status, to_status,
                   transaction_version, reason, source
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                self._id("status-event"),
                transaction_id,
                current.value,
                target.value,
                next_version,
                reason.strip() or "Status changed",
                source,
            ),
        )
        self._audit(
            connection,
            "transaction",
            transaction_id,
            "status_transition",
            {"booking_status": current.value, "version": expected_version},
            {"booking_status": target.value, "version": next_version},
            actor=source,
        )
        return next_version

    def transition_transaction_status(
        self,
        transaction_id: str,
        new_status: str | TransactionStatus,
        expected_version: int,
        *,
        reason: str,
        source: str = "user",
    ) -> int:
        with self.database.connect() as connection:
            return self._transition_transaction_status(
                connection,
                transaction_id,
                new_status,
                expected_version,
                reason=reason,
                source=source,
            )

    def list_transaction_status_history(
        self,
        transaction_id: str,
    ) -> list[dict]:
        self.get_transaction(transaction_id)
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM ledger_transaction_status_events
                       WHERE transaction_id=?
                       ORDER BY transaction_version, id""",
                    (transaction_id,),
                )
            ]

    @staticmethod
    def _validate_relation_transactions(
        relation_type: TransactionRelationType,
        source: sqlite3.Row,
        target: sqlite3.Row,
        fee_amount_minor: int,
    ) -> None:
        if source["id"] == target["id"]:
            raise ValueError("A transaction cannot be related to itself")
        if source["state"] != "active" or target["state"] != "active":
            raise ValueError("Transaction relations require unsplit, active transactions")
        if source["currency"] != target["currency"]:
            raise ValueError("Related transactions must use the same currency")
        source_amount = int(source["amount_minor"])
        target_amount = int(target["amount_minor"])
        if fee_amount_minor < 0:
            raise ValueError("Transfer fee must not be negative")
        if relation_type is TransactionRelationType.TRANSFER:
            if source["account_id"] == target["account_id"]:
                raise ValueError("Transfers require two different accounts")
            if source_amount >= 0 or target_amount <= 0:
                raise ValueError("Transfer source must be negative and target positive")
            if abs(source_amount) != target_amount + fee_amount_minor:
                raise ValueError("Transfer amounts must balance exactly including the fee")
        elif relation_type is TransactionRelationType.REFUND:
            if source_amount >= 0 or target_amount <= 0:
                raise ValueError("Refund source must be negative and target positive")
            if target_amount > abs(source_amount):
                raise ValueError("Refund must not exceed the original expense")
            if fee_amount_minor:
                raise ValueError("Fees are supported only for transfers")
        elif relation_type is TransactionRelationType.REVERSAL:
            if source["account_id"] != target["account_id"]:
                raise ValueError("Reversal transactions require the same account")
            if source_amount != -target_amount:
                raise ValueError("Reversal amounts must cancel exactly")
            if fee_amount_minor:
                raise ValueError("Fees are supported only for transfers")
        elif relation_type is TransactionRelationType.CORRECTION:
            if source["account_id"] != target["account_id"]:
                raise ValueError("Correction transactions require the same account")
            if source_amount == 0 or target_amount == 0:
                raise ValueError("Correction transactions must be non-zero")
            if (source_amount < 0) != (target_amount < 0):
                raise ValueError("Correction transactions must have the same direction")
            if fee_amount_minor:
                raise ValueError("Fees are supported only for transfers")

    def create_transaction_relation(
        self,
        relation_type: str | TransactionRelationType,
        source_transaction_id: str,
        target_transaction_id: str,
        *,
        expected_source_version: int,
        expected_target_version: int,
        score: float = 1.0,
        reasons: list[str] | None = None,
        fee_amount_minor: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        kind = TransactionRelationType(relation_type)
        if not 0 <= score <= 1:
            raise ValueError("Relation score must be between 0 and 1")
        with self.database.connect() as connection:
            source = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?",
                (source_transaction_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?",
                (target_transaction_id,),
            ).fetchone()
            if source is None or target is None:
                missing = source_transaction_id if source is None else target_transaction_id
                raise KeyError(f"Unknown transaction {missing}")
            if int(source["version"]) != expected_source_version:
                raise VersionConflictError(
                    "The source transaction has changed; reload it before writing."
                )
            if int(target["version"]) != expected_target_version:
                raise VersionConflictError(
                    "The target transaction has changed; reload it before writing."
                )
            if (
                source["booking_status"] != TransactionStatus.BOOKED.value
                or target["booking_status"] != TransactionStatus.BOOKED.value
            ):
                raise ValueError("Only booked transactions can receive a relation candidate")
            self._validate_relation_transactions(kind, source, target, fee_amount_minor)
            existing = connection.execute(
                """SELECT * FROM ledger_transaction_relations
                   WHERE relation_type=? AND source_transaction_id=?
                     AND target_transaction_id=?""",
                (kind.value, source_transaction_id, target_transaction_id),
            ).fetchone()
            if existing:
                if existing["status"] == "suggested":
                    connection.execute(
                        """UPDATE ledger_transaction_relations
                           SET score=?, reasons_json=?, fee_amount_minor=?,
                               source_version=?, target_version=?,
                               metadata_json=?, version=version+1,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (
                            score,
                            json.dumps(reasons or [], ensure_ascii=False),
                            fee_amount_minor,
                            expected_source_version,
                            expected_target_version,
                            json.dumps(metadata or {}, sort_keys=True),
                            existing["id"],
                        ),
                    )
                    self._audit(
                        connection,
                        "transaction_relation",
                        existing["id"],
                        "refresh_suggestion",
                        {"version": existing["version"]},
                        {"version": int(existing["version"]) + 1},
                    )
                    existing = connection.execute(
                        """SELECT * FROM ledger_transaction_relations
                           WHERE id=?""",
                        (existing["id"],),
                    ).fetchone()
                return self._relation_record(existing)
            identifier = self._id("transaction-relation")
            connection.execute(
                """INSERT INTO ledger_transaction_relations(
                       id, relation_type, source_transaction_id,
                       target_transaction_id, status, score, reasons_json,
                       fee_amount_minor, source_version, target_version,
                       metadata_json
                   ) VALUES (?, ?, ?, ?, 'suggested', ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    kind.value,
                    source_transaction_id,
                    target_transaction_id,
                    score,
                    json.dumps(reasons or [], ensure_ascii=False),
                    fee_amount_minor,
                    expected_source_version,
                    expected_target_version,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            self._audit(
                connection,
                "transaction_relation",
                identifier,
                "suggest",
                None,
                {
                    "relation_type": kind.value,
                    "source_transaction_id": source_transaction_id,
                    "target_transaction_id": target_transaction_id,
                    "score": score,
                    "fee_amount_minor": fee_amount_minor,
                },
            )
            row = connection.execute(
                "SELECT * FROM ledger_transaction_relations WHERE id=?",
                (identifier,),
            ).fetchone()
            return self._relation_record(row)

    @staticmethod
    def _relation_record(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json") or "[]")
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def list_transaction_relations(
        self,
        *,
        relation_type: str | None = None,
        status: str | None = None,
        transaction_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        parameters: list[object] = []
        if relation_type:
            clauses.append("relation_type=?")
            parameters.append(TransactionRelationType(relation_type).value)
        if status:
            clauses.append("status=?")
            parameters.append(TransactionRelationStatus(status).value)
        if transaction_id:
            clauses.append("(source_transaction_id=? OR target_transaction_id=?)")
            parameters.extend((transaction_id, transaction_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with self.database.connect() as connection:
            return [
                self._relation_record(row)
                for row in connection.execute(
                    "SELECT * FROM ledger_transaction_relations"
                    + where
                    + " ORDER BY updated_at DESC, id DESC LIMIT ?",
                    parameters,
                )
            ]

    def review_transaction_relation(
        self,
        relation_id: str,
        decision: str,
        expected_version: int,
    ) -> dict:
        if decision not in {"confirm", "reject", "resolve"}:
            raise ValueError("Decision must be confirm, reject or resolve")
        with self.database.connect() as connection:
            relation = connection.execute(
                "SELECT * FROM ledger_transaction_relations WHERE id=?",
                (relation_id,),
            ).fetchone()
            if relation is None:
                raise KeyError(f"Unknown transaction relation {relation_id}")
            if int(relation["version"]) != expected_version:
                raise VersionConflictError("The relation has changed; reload it before writing.")
            expected_status = "confirmed" if decision == "resolve" else "suggested"
            if relation["status"] != expected_status:
                raise ValueError(f"Decision {decision} requires relation status {expected_status}")
            next_relation_status = {
                "confirm": "confirmed",
                "reject": "rejected",
                "resolve": "resolved",
            }[decision]
            metadata = json.loads(relation["metadata_json"] or "{}")
            if decision in {"confirm", "resolve"}:
                source = connection.execute(
                    "SELECT * FROM ledger_transactions WHERE id=?",
                    (relation["source_transaction_id"],),
                ).fetchone()
                target = connection.execute(
                    "SELECT * FROM ledger_transactions WHERE id=?",
                    (relation["target_transaction_id"],),
                ).fetchone()
                if int(source["version"]) != int(relation["source_version"]) or int(
                    target["version"]
                ) != int(relation["target_version"]):
                    raise VersionConflictError(
                        "A related transaction has changed; reload the relation."
                    )
                kind = TransactionRelationType(relation["relation_type"])
                self._validate_relation_transactions(
                    kind,
                    source,
                    target,
                    int(relation["fee_amount_minor"]),
                )
                if decision == "confirm":
                    if (
                        source["booking_status"] != TransactionStatus.BOOKED.value
                        or target["booking_status"] != TransactionStatus.BOOKED.value
                    ):
                        raise ValueError("Only booked transactions can confirm a relation")
                    if kind in {
                        TransactionRelationType.TRANSFER,
                        TransactionRelationType.REVERSAL,
                        TransactionRelationType.CORRECTION,
                    }:
                        conflict = connection.execute(
                            """SELECT id FROM ledger_transaction_relations
                               WHERE id<>? AND status='confirmed'
                                 AND relation_type IN
                                     ('transfer', 'reversal', 'correction')
                                 AND (source_transaction_id IN (?, ?)
                                      OR target_transaction_id IN (?, ?))
                               LIMIT 1""",
                            (
                                relation_id,
                                source["id"],
                                target["id"],
                                source["id"],
                                target["id"],
                            ),
                        ).fetchone()
                        if conflict:
                            raise ValueError(
                                "A transaction already has a confirmed exclusive "
                                f"{kind.value} relation"
                            )
                    if kind is TransactionRelationType.REFUND:
                        target_conflict = connection.execute(
                            """SELECT id FROM ledger_transaction_relations
                               WHERE id<>? AND status='confirmed'
                                 AND (source_transaction_id=?
                                      OR target_transaction_id=?)
                               LIMIT 1""",
                            (relation_id, target["id"], target["id"]),
                        ).fetchone()
                        source_exclusive = connection.execute(
                            """SELECT id FROM ledger_transaction_relations
                               WHERE id<>? AND status='confirmed'
                                 AND relation_type IN
                                     ('transfer', 'reversal', 'correction')
                                 AND (source_transaction_id=?
                                      OR target_transaction_id=?)
                               LIMIT 1""",
                            (relation_id, source["id"], source["id"]),
                        ).fetchone()
                        refunded = connection.execute(
                            """SELECT COALESCE(SUM(target.amount_minor), 0)
                               FROM ledger_transaction_relations existing
                               JOIN ledger_transactions target
                                 ON target.id=existing.target_transaction_id
                               WHERE existing.id<>?
                                 AND existing.status='confirmed'
                                 AND existing.relation_type='refund'
                                 AND existing.source_transaction_id=?""",
                            (relation_id, source["id"]),
                        ).fetchone()[0]
                        if target_conflict or source_exclusive:
                            raise ValueError(
                                "Refund transactions conflict with an existing confirmed relation"
                            )
                        if int(refunded) + int(target["amount_minor"]) > abs(
                            int(source["amount_minor"])
                        ):
                            raise ValueError(
                                "Confirmed refunds must not exceed the original expense"
                            )
                if decision == "confirm" and kind in {
                    TransactionRelationType.REVERSAL,
                    TransactionRelationType.CORRECTION,
                }:
                    terminal = (
                        TransactionStatus.REVERSED
                        if kind is TransactionRelationType.REVERSAL
                        else TransactionStatus.CORRECTED
                    )
                    metadata["source_status_before_confirmation"] = source["booking_status"]
                    next_source_version = self._transition_transaction_status(
                        connection,
                        source["id"],
                        terminal,
                        int(source["version"]),
                        reason=f"Confirmed {kind.value}-Beziehung {relation_id}",
                        source="relation",
                    )
                elif decision == "resolve" and metadata.get("source_status_before_confirmation"):
                    next_source_version = self._transition_transaction_status(
                        connection,
                        source["id"],
                        metadata["source_status_before_confirmation"],
                        int(source["version"]),
                        reason=f"Beziehung {relation_id} unlinked",
                        source="relation",
                        allow_terminal_reset=True,
                    )
                else:
                    next_source_version = int(source["version"]) + 1
                    connection.execute(
                        """UPDATE ledger_transactions SET version=?,
                                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (next_source_version, source["id"]),
                    )
                next_target_version = int(target["version"]) + 1
                connection.execute(
                    """UPDATE ledger_transactions SET version=?,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (next_target_version, target["id"]),
                )
                for item, next_version in (
                    (source, next_source_version),
                    (target, next_target_version),
                ):
                    self._audit(
                        connection,
                        "transaction",
                        item["id"],
                        f"relation_{decision}",
                        {"version": item["version"]},
                        {
                            "version": next_version,
                            "relation_id": relation_id,
                            "relation_type": kind.value,
                        },
                    )
            else:
                next_source_version = int(relation["source_version"])
                next_target_version = int(relation["target_version"])
            connection.execute(
                """UPDATE ledger_transaction_relations
                   SET status=?, source_version=?, target_version=?,
                       version=version+1, metadata_json=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    next_relation_status,
                    next_source_version,
                    next_target_version,
                    json.dumps(metadata, sort_keys=True),
                    relation_id,
                ),
            )
            self._audit(
                connection,
                "transaction_relation",
                relation_id,
                decision,
                {"status": relation["status"], "version": expected_version},
                {
                    "status": next_relation_status,
                    "version": expected_version + 1,
                },
            )
            updated = connection.execute(
                "SELECT * FROM ledger_transaction_relations WHERE id=?",
                (relation_id,),
            ).fetchone()
            return self._relation_record(updated)

    def list_accounts(self, *, enabled_only: bool = False) -> list[dict]:
        query = """SELECT a.*, p.kind AS provider_kind,
                          p.display_name AS provider_name
                   FROM ledger_accounts a
                   JOIN ledger_providers p ON p.id=a.provider_id"""
        parameters: list[object] = []
        if enabled_only:
            query += " WHERE a.enabled=1"
        query += " ORDER BY p.display_name, a.display_name, a.id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def get_account(self, account_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT a.*, p.kind AS provider_kind,
                          p.display_name AS provider_name
                   FROM ledger_accounts a
                   JOIN ledger_providers p ON p.id=a.provider_id
                   WHERE a.id=?""",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown account {account_id}")
        return dict(row)

    def update_account_alias(
        self,
        account_id: str,
        display_name: str,
        *,
        expected_version: int | None = None,
    ) -> int:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("Account alias must not be empty")
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT display_name, version FROM ledger_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {account_id}")
            if expected_version is not None and account["version"] != expected_version:
                raise VersionConflictError("The account has changed; reload it before writing.")
            connection.execute(
                """UPDATE ledger_accounts
                   SET display_name=?, version=version+1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (normalized_name, account_id),
            )
            next_version = int(account["version"]) + 1
            self._audit(
                connection,
                "account",
                account_id,
                "alias",
                {
                    "display_name": account["display_name"],
                    "version": account["version"],
                },
                {
                    "display_name": normalized_name,
                    "version": next_version,
                },
            )
            return next_version

    def set_account_enabled(
        self,
        account_id: str,
        enabled: bool,
        *,
        expected_version: int | None = None,
    ) -> int:
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT enabled, version FROM ledger_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {account_id}")
            if expected_version is not None and account["version"] != expected_version:
                raise VersionConflictError("The account has changed; reload it before writing.")
            if bool(account["enabled"]) == enabled:
                return int(account["version"])
            connection.execute(
                """UPDATE ledger_accounts
                   SET enabled=?, version=version+1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (int(enabled), account_id),
            )
            next_version = int(account["version"]) + 1
            self._audit(
                connection,
                "account",
                account_id,
                "enable" if enabled else "disable",
                {
                    "enabled": bool(account["enabled"]),
                    "version": account["version"],
                },
                {"enabled": enabled, "version": next_version},
            )
            return next_version

    def resolve_account_scope(self, scope: AccountScope) -> tuple[str, ...]:
        with self.database.connect() as connection:
            if scope.mode == "all":
                return tuple(
                    row["id"]
                    for row in connection.execute(
                        """SELECT id FROM ledger_accounts
                           WHERE enabled=1 ORDER BY id"""
                    )
                )
            return self._resolve_selected_account_ids(connection, scope.account_ids)

    @staticmethod
    def _resolve_selected_account_ids(
        connection: sqlite3.Connection,
        account_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        placeholders = ",".join("?" for _ in account_ids)
        rows = connection.execute(
            f"""SELECT id, enabled FROM ledger_accounts
                WHERE id IN ({placeholders})""",
            account_ids,
        ).fetchall()
        found = {row["id"]: bool(row["enabled"]) for row in rows}
        missing = [identifier for identifier in account_ids if identifier not in found]
        disabled = [identifier for identifier in account_ids if not found.get(identifier, True)]
        if missing:
            raise KeyError("Unknown accounts: " + ", ".join(missing))
        if disabled:
            raise ValueError("Disabled accounts selected: " + ", ".join(disabled))
        return account_ids

    def create_account_group(
        self,
        name: str,
        account_ids: list[str],
        description: str = "",
    ) -> str:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Account group name must not be empty")
        scope = AccountScope.selected(account_ids)
        resolved_ids = self.resolve_account_scope(scope)
        identifier = self._id("account-group")
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM ledger_account_groups WHERE name=?",
                (normalized_name,),
            ).fetchone():
                raise ValueError(f"Account group already exists: {normalized_name}")
            connection.execute(
                """INSERT INTO ledger_account_groups(id, name, description)
                   VALUES (?, ?, ?)""",
                (identifier, normalized_name, description.strip()),
            )
            connection.executemany(
                """INSERT INTO ledger_account_group_members(group_id, account_id)
                   VALUES (?, ?)""",
                [(identifier, account_id) for account_id in resolved_ids],
            )
            self._audit(
                connection,
                "account_group",
                identifier,
                "create",
                None,
                {
                    "name": normalized_name,
                    "description": description.strip(),
                    "account_ids": list(resolved_ids),
                },
            )
        return identifier

    def list_account_groups(self) -> list[dict]:
        with self.database.connect() as connection:
            groups = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM ledger_account_groups
                       ORDER BY name, id"""
                )
            ]
            for group in groups:
                group["account_ids"] = [
                    row["account_id"]
                    for row in connection.execute(
                        """SELECT account_id
                           FROM ledger_account_group_members
                           WHERE group_id=? ORDER BY account_id""",
                        (group["id"],),
                    )
                ]
        return groups

    def update_account_group_members(
        self,
        name_or_id: str,
        account_ids: list[str],
        *,
        expected_version: int,
    ) -> int:
        scope = AccountScope.selected(account_ids)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute(
                "SELECT * FROM ledger_account_groups WHERE id=?",
                (name_or_id,),
            ).fetchone()
            if group is None:
                group = connection.execute(
                    "SELECT * FROM ledger_account_groups WHERE name=?",
                    (name_or_id,),
                ).fetchone()
            if group is None:
                raise KeyError(f"Unknown account group {name_or_id}")
            if group["version"] != expected_version:
                raise VersionConflictError(
                    "The account group has changed; reload it before writing."
                )
            resolved_ids = self._resolve_selected_account_ids(
                connection,
                scope.account_ids,
            )
            previous_ids = [
                row["account_id"]
                for row in connection.execute(
                    """SELECT account_id FROM ledger_account_group_members
                       WHERE group_id=? ORDER BY account_id""",
                    (group["id"],),
                )
            ]
            next_ids = sorted(resolved_ids)
            if previous_ids == next_ids:
                return expected_version
            next_version = expected_version + 1
            updated = connection.execute(
                """UPDATE ledger_account_groups
                   SET version=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND version=?""",
                (next_version, group["id"], expected_version),
            )
            if updated.rowcount != 1:
                raise VersionConflictError(
                    "The account group has changed; reload it before writing."
                )
            connection.execute(
                "DELETE FROM ledger_account_group_members WHERE group_id=?",
                (group["id"],),
            )
            connection.executemany(
                """INSERT INTO ledger_account_group_members(group_id, account_id)
                   VALUES (?, ?)""",
                [(group["id"], account_id) for account_id in resolved_ids],
            )
            self._audit(
                connection,
                "account_group",
                group["id"],
                "update_members",
                {"account_ids": previous_ids, "version": expected_version},
                {"account_ids": next_ids, "version": next_version},
            )
        return next_version

    def account_scope_for_group(self, name_or_id: str) -> AccountScope:
        with self.database.connect() as connection:
            group = connection.execute(
                """SELECT id FROM ledger_account_groups
                   WHERE id=? OR name=?""",
                (name_or_id, name_or_id),
            ).fetchone()
            if group is None:
                raise KeyError(f"Unknown account group {name_or_id}")
            account_ids = [
                row["account_id"]
                for row in connection.execute(
                    """SELECT account_id
                       FROM ledger_account_group_members
                       WHERE group_id=? ORDER BY account_id""",
                    (group["id"],),
                )
            ]
        scope = AccountScope.selected(account_ids)
        self.resolve_account_scope(scope)
        return scope

    def update_account_balance(
        self,
        account_id: str,
        balance_minor: int | None,
        *,
        balance_date: str | date | None = None,
        source: str = "provider",
        external_id: str | None = None,
    ) -> dict | None:
        if balance_minor is not None:
            return self.record_balance_snapshot(
                account_id,
                balance_minor,
                balance_date=balance_date,
                source=source,
                external_id=external_id,
            )
        with self.database.connect() as connection:
            account = connection.execute(
                """SELECT last_balance_minor, last_balance_at, version
                   FROM ledger_accounts WHERE id=?""",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {account_id}")
            if account["last_balance_minor"] == balance_minor:
                return None
            connection.execute(
                """UPDATE ledger_accounts
                   SET last_balance_minor=NULL, last_balance_at=NULL,
                       version=version+1,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (account_id,),
            )
            self._audit(
                connection,
                "account",
                account_id,
                "balance",
                {
                    "last_balance_minor": account["last_balance_minor"],
                    "last_balance_at": account["last_balance_at"],
                },
                {"last_balance_minor": None, "last_balance_at": None},
            )
        return None

    def record_balance_snapshot(
        self,
        account_id: str,
        balance_minor: int,
        *,
        balance_date: str | date | None = None,
        source: str = "manual",
        external_id: str | None = None,
        metadata: dict | None = None,
        expected_account_version: int | None = None,
    ) -> dict:
        if not source.strip():
            raise ValueError("Balance source must not be empty")
        if balance_date is None:
            normalized_date = datetime.now(UTC).date().isoformat()
        elif isinstance(balance_date, date):
            normalized_date = balance_date.isoformat()
        else:
            normalized_date = date.fromisoformat(balance_date).isoformat()
        with self.database.connect() as connection:
            account = connection.execute(
                """SELECT id, currency, last_balance_minor,
                          last_balance_at, version
                   FROM ledger_accounts WHERE id=?""",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {account_id}")
            if (
                expected_account_version is not None
                and int(account["version"]) != expected_account_version
            ):
                raise VersionConflictError("The account has changed; reload it before writing.")
            idempotency_key = hashlib.sha256(
                (
                    f"{account_id}|{normalized_date}|{balance_minor}|"
                    f"{account['currency']}|{source.strip()}|{external_id or ''}"
                ).encode()
            ).hexdigest()
            existing = connection.execute(
                """SELECT * FROM ledger_balance_snapshots
                   WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["created"] = False
                snapshot_id = existing["id"]
            else:
                snapshot_id = self._id("balance-snapshot")
                connection.execute(
                    """INSERT INTO ledger_balance_snapshots(
                           id, account_id, balance_date, balance_minor,
                           currency, source, external_id, idempotency_key,
                           metadata_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        account_id,
                        normalized_date,
                        balance_minor,
                        account["currency"],
                        source.strip(),
                        external_id,
                        idempotency_key,
                        json.dumps(metadata or {}, sort_keys=True),
                    ),
                )
                current_date = (
                    str(account["last_balance_at"])[:10] if account["last_balance_at"] else None
                )
                if current_date is None or normalized_date >= current_date:
                    connection.execute(
                        """UPDATE ledger_accounts
                           SET last_balance_minor=?, last_balance_at=?,
                               version=version+1,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (balance_minor, normalized_date, account_id),
                    )
                self._audit(
                    connection,
                    "balance_snapshot",
                    snapshot_id,
                    "record",
                    None,
                    {
                        "account_id": account_id,
                        "balance_date": normalized_date,
                        "balance_minor": balance_minor,
                        "currency": account["currency"],
                        "source": source.strip(),
                    },
                )
                result = dict(
                    connection.execute(
                        "SELECT * FROM ledger_balance_snapshots WHERE id=?",
                        (snapshot_id,),
                    ).fetchone()
                )
                result["created"] = True
        result["reconciliation"] = self.reconcile_balance_snapshot(snapshot_id)
        return result

    def list_balance_snapshots(
        self,
        *,
        account_id: str | None = None,
        active: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT * FROM ledger_balance_snapshots"
        parameters: list[object] = []
        if account_id:
            query += " WHERE account_id=?"
            parameters.append(account_id)
        query += " ORDER BY balance_date DESC, created_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def reconcile_balance_snapshot(
        self,
        snapshot_id: str,
        *,
        refresh: bool = False,
    ) -> dict:
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ledger_reconciliations WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing and not refresh:
                return dict(existing)
            snapshot = connection.execute(
                "SELECT * FROM ledger_balance_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"Unknown balance snapshot {snapshot_id}")
            previous = connection.execute(
                """SELECT * FROM ledger_balance_snapshots
                   WHERE account_id=? AND currency=? AND id<>?
                     AND (balance_date < ? OR
                          (balance_date = ? AND created_at < ?))
                   ORDER BY balance_date DESC, created_at DESC, id DESC
                   LIMIT 1""",
                (
                    snapshot["account_id"],
                    snapshot["currency"],
                    snapshot_id,
                    snapshot["balance_date"],
                    snapshot["balance_date"],
                    snapshot["created_at"],
                ),
            ).fetchone()
            if previous is None:
                calculated = difference = None
                transaction_count = 0
                status = "baseline"
            else:
                movement = connection.execute(
                    """SELECT COALESCE(SUM(amount_minor), 0) AS amount,
                              COUNT(*) AS transaction_count
                       FROM ledger_transactions
                       WHERE account_id=? AND state<>'void'
                         AND (
                           booking_status='booked'
                           OR (booking_status='reversed' AND EXISTS (
                             SELECT 1
                             FROM ledger_transaction_relations relation
                             WHERE relation.status='confirmed'
                               AND relation.relation_type='reversal'
                               AND relation.source_transaction_id=
                                   ledger_transactions.id
                           ))
                         )
                         AND booking_date>? AND booking_date<=?""",
                    (
                        snapshot["account_id"],
                        previous["balance_date"],
                        snapshot["balance_date"],
                    ),
                ).fetchone()
                calculated = int(previous["balance_minor"]) + int(movement["amount"])
                difference = int(snapshot["balance_minor"]) - calculated
                transaction_count = int(movement["transaction_count"])
                status = "balanced" if difference == 0 else "difference"
            identifier = existing["id"] if existing else self._id("reconciliation")
            before = dict(existing) if existing else None
            if existing:
                connection.execute(
                    """UPDATE ledger_reconciliations
                       SET previous_snapshot_id=?, calculated_balance_minor=?,
                           difference_minor=?, transaction_count=?, status=?,
                           version=version+1,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        previous["id"] if previous else None,
                        calculated,
                        difference,
                        transaction_count,
                        status,
                        identifier,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO ledger_reconciliations(
                           id, account_id, snapshot_id, previous_snapshot_id,
                           calculated_balance_minor, difference_minor,
                           transaction_count, status
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identifier,
                        snapshot["account_id"],
                        snapshot_id,
                        previous["id"] if previous else None,
                        calculated,
                        difference,
                        transaction_count,
                        status,
                    ),
                )
            self._audit(
                connection,
                "reconciliation",
                identifier,
                "recalculate" if existing else "calculate",
                before,
                {
                    "snapshot_id": snapshot_id,
                    "previous_snapshot_id": previous["id"] if previous else None,
                    "calculated_balance_minor": calculated,
                    "difference_minor": difference,
                    "status": status,
                },
            )
            return dict(
                connection.execute(
                    "SELECT * FROM ledger_reconciliations WHERE id=?",
                    (identifier,),
                ).fetchone()
            )

    def list_reconciliations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        query = """SELECT r.*, s.balance_date, s.balance_minor, s.currency,
                          s.source AS balance_source
                   FROM ledger_reconciliations r
                   JOIN ledger_balance_snapshots s ON s.id=r.snapshot_id"""
        parameters: list[object] = []
        if account_id:
            query += " WHERE r.account_id=?"
            parameters.append(account_id)
        query += " ORDER BY s.balance_date DESC, r.created_at DESC LIMIT ?"
        parameters.append(limit)
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def start_sync_run(
        self,
        provider_id: str,
        account_ids: list[str],
        booking_from: str | None,
        booking_to: str | None,
        *,
        cursor: dict[str, int] | None = None,
        account_cursors: dict[str, int] | None = None,
    ) -> str:
        identifier = self._id("sync")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_sync_runs(
                       id, provider_id, status, account_ids_json,
                       booking_from, booking_to, cursor_json
                   )
                   VALUES (?, ?, 'running', ?, ?, ?, ?)""",
                (
                    identifier,
                    provider_id,
                    json.dumps(account_ids),
                    booking_from,
                    booking_to,
                    json.dumps(cursor or {}, sort_keys=True),
                ),
            )
            connection.executemany(
                """INSERT INTO ledger_sync_account_runs(
                       sync_run_id, account_id, status, cursor
                   )
                   VALUES (?, ?, 'running', ?)""",
                [
                    (
                        identifier,
                        account_id,
                        int((account_cursors or {}).get(account_id, 0)),
                    )
                    for account_id in account_ids
                ],
            )
            self._audit(
                connection,
                "sync_run",
                identifier,
                "start",
                None,
                {
                    "provider_id": provider_id,
                    "account_ids": account_ids,
                    "booking_from": booking_from,
                    "booking_to": booking_to,
                    "cursor": cursor or {},
                },
            )
        return identifier

    def update_sync_run(
        self,
        sync_run_id: str,
        *,
        account_id: str,
        account_cursor: int,
        cursor: dict[str, int],
        imported_increment: int,
        skipped_increment: int,
        updated_increment: int = 0,
    ) -> None:
        with self.database.connect() as connection:
            run = connection.execute(
                """SELECT status, imported_count, skipped_count, cursor_json
                   FROM ledger_sync_runs WHERE id=?""",
                (sync_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown sync run {sync_run_id}")
            if run["status"] != "running":
                raise ValueError("Only running sync runs can be updated")
            connection.execute(
                """UPDATE ledger_sync_runs
                   SET cursor_json=?,
                       imported_count=imported_count+?,
                       skipped_count=skipped_count+?,
                       updated_count=updated_count+?
                   WHERE id=?""",
                (
                    json.dumps(cursor, sort_keys=True),
                    imported_increment,
                    skipped_increment,
                    updated_increment,
                    sync_run_id,
                ),
            )
            connection.execute(
                """UPDATE ledger_sync_account_runs
                   SET cursor=?, imported_count=imported_count+?,
                       skipped_count=skipped_count+?,
                       updated_count=updated_count+?
                   WHERE sync_run_id=? AND account_id=?""",
                (
                    account_cursor,
                    imported_increment,
                    skipped_increment,
                    updated_increment,
                    sync_run_id,
                    account_id,
                ),
            )

    def finish_sync_run(
        self,
        sync_run_id: str,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("Sync run must finish as completed or failed")
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT * FROM ledger_sync_runs WHERE id=?",
                (sync_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown sync run {sync_run_id}")
            connection.execute(
                """UPDATE ledger_sync_runs
                   SET status=?, error_code=?, finished_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (status, error_code, sync_run_id),
            )
            connection.execute(
                """UPDATE ledger_sync_account_runs
                   SET status=?, error_code=?,
                       finished_at=CURRENT_TIMESTAMP
                   WHERE sync_run_id=? AND status='running'""",
                (status, error_code, sync_run_id),
            )
            self._audit(
                connection,
                "sync_run",
                sync_run_id,
                status,
                {
                    "status": run["status"],
                    "cursor": json.loads(run["cursor_json"]),
                },
                {
                    "status": status,
                    "error_code": error_code,
                    "imported_count": run["imported_count"],
                    "skipped_count": run["skipped_count"],
                    "updated_count": run["updated_count"],
                },
            )

    def get_sync_run(self, sync_run_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_sync_runs WHERE id=?",
                (sync_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown sync run {sync_run_id}")
        result = dict(row)
        result["account_ids"] = json.loads(result.pop("account_ids_json"))
        result["cursor"] = json.loads(result.pop("cursor_json"))
        with self.database.connect() as connection:
            result["account_runs"] = [
                dict(account_run)
                for account_run in connection.execute(
                    """SELECT * FROM ledger_sync_account_runs
                       WHERE sync_run_id=? ORDER BY account_id""",
                    (sync_run_id,),
                )
            ]
        return result

    def list_sync_runs(
        self,
        provider_kind: str | None = None,
        *,
        provider_name: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        query = (
            "SELECT r.*, p.kind AS provider_kind, p.display_name AS provider_name "
            "FROM ledger_sync_runs r JOIN ledger_providers p ON p.id=r.provider_id"
        )
        parameters: list[object] = []
        filters: list[str] = []
        if provider_kind:
            filters.append("p.kind=?")
            parameters.append(provider_kind)
        if provider_name:
            filters.append("p.display_name=?")
            parameters.append(provider_name)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY r.started_at DESC LIMIT ?"
        parameters.append(limit)
        with self.database.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, parameters)]
        for row in rows:
            row["account_ids"] = json.loads(row.pop("account_ids_json"))
            row["cursor"] = json.loads(row.pop("cursor_json"))
        return rows

    def existing_source_identities(
        self,
        provider_kind: str,
        provider_name: str,
        identities: list[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        if not identities:
            return set()
        with self.database.connect() as connection:
            provider = connection.execute(
                """SELECT id FROM ledger_providers
                   WHERE kind=? AND display_name=?""",
                (provider_kind, provider_name),
            ).fetchone()
            if provider is None:
                return set()
            existing: set[tuple[str, str]] = set()
            for account_external_id, external_id in identities:
                row = connection.execute(
                    """SELECT 1
                       FROM ledger_source_transactions st
                       JOIN ledger_accounts a ON a.id=st.account_id
                       WHERE st.provider_id=? AND a.external_id=?
                         AND st.external_id=?""",
                    (
                        provider["id"],
                        account_external_id,
                        external_id,
                    ),
                ).fetchone()
                if row:
                    existing.add((account_external_id, external_id))
            return existing

    def create_import_preview(
        self,
        *,
        source: str,
        file_path: str,
        file_hash: str,
        account_id: str | None,
        mapper_version: str,
        summary: dict,
    ) -> str:
        identifier = self._id("import-preview")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO ledger_import_previews(
                       id, source, file_path, file_hash, account_id, status,
                       mapper_version, summary_json
                   )
                   VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    identifier,
                    source,
                    file_path,
                    file_hash,
                    account_id,
                    mapper_version,
                    json.dumps(summary, sort_keys=True, default=str),
                ),
            )
            self._audit(
                connection,
                "import_preview",
                identifier,
                "prepare",
                None,
                {
                    "source": source,
                    "file_hash": file_hash,
                    "account_id": account_id,
                    "mapper_version": mapper_version,
                    "summary": summary,
                },
            )
            if int(summary.get("duplicate_candidates") or 0) > 0:
                self._enqueue_review_item(
                    connection,
                    "duplicate_candidate",
                    "import_preview",
                    identifier,
                    (
                        f"{summary['duplicate_candidates']} possible duplicate(s) "
                        "to review in the import preview"
                    ),
                    75,
                )
            warnings = [str(item) for item in summary.get("warnings") or ()]
            if warnings:
                self._enqueue_review_item(
                    connection,
                    "import_warning",
                    "import_preview",
                    identifier,
                    "; ".join(warnings),
                    65,
                )
            error_count = int(summary.get("error_count") or 0)
            if error_count:
                self._enqueue_review_item(
                    connection,
                    "processing_error",
                    "import_preview",
                    identifier,
                    f"{error_count} structured import errors prevent execution",
                    95,
                )
        return identifier

    def get_import_preview(self, preview_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_import_previews WHERE id=?",
                (preview_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown import preview {preview_id}")
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        return result

    def complete_import_preview(
        self,
        preview_id: str,
        result: ImportResult,
    ) -> str:
        run_id = self._id("import-run")
        with self.database.connect() as connection:
            preview = connection.execute(
                "SELECT status FROM ledger_import_previews WHERE id=?",
                (preview_id,),
            ).fetchone()
            if preview is None:
                raise KeyError(f"Unknown import preview {preview_id}")
            if preview["status"] != "prepared":
                raise ValueError("Import preview has already been consumed")
            connection.execute(
                """INSERT INTO ledger_import_runs(
                       id, preview_id, imported_count, skipped_count,
                       updated_count, account_count
                   )
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    preview_id,
                    result.imported,
                    result.skipped,
                    result.updated,
                    result.accounts,
                ),
            )
            connection.execute(
                """UPDATE ledger_import_previews
                   SET status='executed', executed_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (preview_id,),
            )
            self._audit(
                connection,
                "import_preview",
                preview_id,
                "execute",
                {"status": "prepared"},
                {
                    "status": "executed",
                    "run_id": run_id,
                    "imported": result.imported,
                    "skipped": result.skipped,
                    "updated": result.updated,
                    "accounts": result.accounts,
                },
            )
        return run_id

    def fail_import_preview(self, preview_id: str, error_code: str) -> None:
        with self.database.connect() as connection:
            preview = connection.execute(
                "SELECT status FROM ledger_import_previews WHERE id=?",
                (preview_id,),
            ).fetchone()
            if preview is None:
                raise KeyError(f"Unknown import preview {preview_id}")
            if preview["status"] != "prepared":
                return
            connection.execute(
                """UPDATE ledger_import_previews
                   SET status='failed', executed_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (preview_id,),
            )
            self._audit(
                connection,
                "import_preview",
                preview_id,
                "fail",
                {"status": "prepared"},
                {"status": "failed", "error_code": error_code},
            )
            self._enqueue_review_item(
                connection,
                "processing_error",
                "import_preview",
                preview_id,
                f"Import fehlgeschlagen: {error_code}",
                95,
            )

    def ensure_profile(self, code: str, name: str, description: str = "") -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM ledger_profiles WHERE code=?", (code,)
            ).fetchone()
            if row:
                return row["id"]
            identifier = self._id("profile")
            connection.execute(
                "INSERT INTO ledger_profiles(id, code, name, description) VALUES (?, ?, ?, ?)",
                (identifier, code, name, description),
            )
            self._audit(
                connection,
                "profile",
                identifier,
                "create",
                None,
                {"code": code, "name": name, "description": description},
            )
            return identifier

    def ensure_category(self, code: str, name: str, parent_code: str | None = None) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM ledger_categories WHERE code=?", (code,)
            ).fetchone()
            if row:
                return row["id"]
            parent_id = None
            if parent_code:
                parent = connection.execute(
                    "SELECT id FROM ledger_categories WHERE code=?", (parent_code,)
                ).fetchone()
                parent_id = parent["id"] if parent else None
            identifier = self._id("category")
            connection.execute(
                "INSERT INTO ledger_categories(id, parent_id, code, name) VALUES (?, ?, ?, ?)",
                (identifier, parent_id, code, name),
            )
            self._audit(
                connection,
                "category",
                identifier,
                "create",
                None,
                {
                    "code": code,
                    "name": name,
                    "parent_code": parent_code,
                },
            )
            return identifier

    def list_profiles(self) -> list[dict]:
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM ledger_profiles ORDER BY code")
            ]

    def update_profile(self, code: str, name: str, description: str) -> dict:
        with self.database.connect() as connection:
            profile = connection.execute(
                "SELECT * FROM ledger_profiles WHERE code=?", (code,)
            ).fetchone()
            if profile is None:
                raise KeyError(f"Unknown profile {code}")
            connection.execute(
                "UPDATE ledger_profiles SET name=?, description=? WHERE id=?",
                (name.strip(), description.strip(), profile["id"]),
            )
            result = {**dict(profile), "name": name.strip(), "description": description.strip()}
            self._audit(connection, "profile", profile["id"], "update", dict(profile), result)
            return result

    def add_profile_category(self, profile_code: str, code: str, name: str) -> dict:
        with self.database.connect() as connection:
            profile = connection.execute(
                "SELECT * FROM ledger_profiles WHERE code=?", (profile_code,)
            ).fetchone()
            if profile is None:
                raise KeyError(f"Unknown profile {profile_code}")
            category = connection.execute(
                "SELECT * FROM ledger_categories WHERE code=?", (code,)
            ).fetchone()
            if category is None:
                category_id = self._id("category")
                connection.execute(
                    "INSERT INTO ledger_categories(id, code, name) VALUES (?, ?, ?)",
                    (category_id, code, name.strip()),
                )
                category = connection.execute(
                    "SELECT * FROM ledger_categories WHERE id=?", (category_id,)
                ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO ledger_profile_categories(profile_id, category_id) VALUES (?, ?)",
                (profile["id"], category["id"]),
            )
            result = {
                "profile_code": profile_code,
                "code": category["code"],
                "name": category["name"],
            }
            self._audit(connection, "profile", profile["id"], "category_add", None, result)
            return result

    def remove_profile_category(self, profile_code: str, code: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT pc.profile_id, pc.category_id FROM ledger_profile_categories pc
                   JOIN ledger_profiles p ON p.id=pc.profile_id
                   JOIN ledger_categories c ON c.id=pc.category_id WHERE p.code=? AND c.code=?""",
                (profile_code, code),
            ).fetchone()
            if row is None:
                raise KeyError(f"Category {code} is not part of profile {profile_code}")
            uses = connection.execute(
                "SELECT count(*) FROM ledger_classifications WHERE profile_id=? AND category_id=?",
                (row["profile_id"], row["category_id"]),
            ).fetchone()[0]
            if uses:
                raise ValueError(f"Category {code} is used by {uses} classifications")
            connection.execute(
                "DELETE FROM ledger_profile_categories WHERE profile_id=? AND category_id=?",
                (row["profile_id"], row["category_id"]),
            )
            self._audit(
                connection, "profile", row["profile_id"], "category_remove", {"code": code}, None
            )

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"Invalid regular expression: {error}") from error

    def _rule_targets(
        self,
        connection: sqlite3.Connection,
        profile_code: str,
        category_code: str,
        account_id: str | None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        profile = connection.execute(
            "SELECT * FROM ledger_profiles WHERE code=?", (profile_code,)
        ).fetchone()
        if profile is None:
            raise KeyError(f"Unknown profile {profile_code}")
        category = connection.execute(
            """SELECT c.* FROM ledger_categories c JOIN ledger_profile_categories pc ON pc.category_id=c.id
               WHERE pc.profile_id=? AND c.code=?""",
            (profile["id"], category_code),
        ).fetchone()
        if category is None:
            raise ValueError(f"Category {category_code} does not belong to profile {profile_code}")
        if (
            account_id is not None
            and not connection.execute(
                "SELECT 1 FROM ledger_accounts WHERE id=?", (account_id,)
            ).fetchone()
        ):
            raise KeyError(f"Unknown account {account_id}")
        return profile, category

    def add_rule(
        self,
        profile_code: str,
        name: str,
        pattern: str,
        category_code: str,
        priority: int = 100,
        account_id: str | None = None,
        active: bool = True,
    ) -> str:
        self._validate_pattern(pattern)
        identifier = self._id("rule")
        with self.database.connect() as connection:
            profile, category = self._rule_targets(
                connection, profile_code, category_code, account_id
            )
            connection.execute(
                """INSERT INTO ledger_rules(id, profile_id, name, pattern, category_id, priority, account_id, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    profile["id"],
                    name.strip(),
                    pattern,
                    category["id"],
                    priority,
                    account_id,
                    int(active),
                ),
            )
            self._audit(
                connection,
                "rule",
                identifier,
                "create",
                None,
                {
                    "profile": profile_code,
                    "pattern": pattern,
                    "category": category_code,
                    "account_id": account_id,
                },
            )
        return identifier

    def list_rules(
        self,
        *,
        profile_code: str | None = None,
        account_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[dict]:
        query = """SELECT r.*, p.code AS profile_code, c.code AS category_code,
                          a.display_name AS account_display_name
                   FROM ledger_rules r JOIN ledger_profiles p ON p.id=r.profile_id
                   JOIN ledger_categories c ON c.id=r.category_id
                   LEFT JOIN ledger_accounts a ON a.id=r.account_id WHERE 1=1"""
        parameters: list[object] = []
        if profile_code:
            query += " AND p.code=?"
            parameters.append(profile_code)
        if account_id:
            query += " AND (r.account_id IS NULL OR r.account_id=?)"
            parameters.append(account_id)
        if not include_inactive:
            query += " AND r.active=1"
        query += " ORDER BY p.code, r.priority, r.created_at, r.id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def update_rule(
        self,
        rule_id: str,
        *,
        expected_version: int,
        name: str | None = None,
        pattern: str | None = None,
        category_code: str | None = None,
        priority: int | None = None,
        active: bool | None = None,
        account_id: object = _UNSET,
    ) -> dict:
        with self.database.connect() as connection:
            rule = connection.execute(
                """SELECT r.*, p.code AS profile_code, c.code AS category_code FROM ledger_rules r
                   JOIN ledger_profiles p ON p.id=r.profile_id JOIN ledger_categories c ON c.id=r.category_id WHERE r.id=?""",
                (rule_id,),
            ).fetchone()
            if rule is None:
                raise KeyError(f"Unknown rule {rule_id}")
            if rule["version"] != expected_version:
                raise VersionConflictError("The rule has changed; reload it before writing.")
            next_pattern = pattern if pattern is not None else rule["pattern"]
            self._validate_pattern(next_pattern)
            next_category = category_code if category_code is not None else rule["category_code"]
            next_account_id = cast(
                str | None, rule["account_id"] if account_id is _UNSET else account_id
            )
            _profile, category = self._rule_targets(
                connection, rule["profile_code"], next_category, next_account_id
            )
            values = (
                name.strip() if name is not None else rule["name"],
                next_pattern,
                category["id"],
                priority if priority is not None else rule["priority"],
                int(active) if active is not None else rule["active"],
                next_account_id,
            )
            if values == (
                rule["name"],
                rule["pattern"],
                rule["category_id"],
                rule["priority"],
                rule["active"],
                rule["account_id"],
            ):
                return {**dict(rule), "changed": False}
            connection.execute(
                """UPDATE ledger_rules SET name=?, pattern=?, category_id=?, priority=?, active=?, account_id=?, version=version+1 WHERE id=?""",
                (*values, rule_id),
            )
            result = {
                **dict(rule),
                "name": values[0],
                "pattern": values[1],
                "category_id": values[2],
                "category_code": next_category,
                "priority": values[3],
                "active": values[4],
                "account_id": values[5],
                "version": expected_version + 1,
                "changed": True,
            }
            self._audit(connection, "rule", rule_id, "update", dict(rule), result)
            return result

    def delete_rule(self, rule_id: str, expected_version: int) -> None:
        with self.database.connect() as connection:
            rule = connection.execute(
                "SELECT * FROM ledger_rules WHERE id=?", (rule_id,)
            ).fetchone()
            if rule is None:
                raise KeyError(f"Unknown rule {rule_id}")
            if rule["version"] != expected_version:
                raise VersionConflictError("The rule has changed; reload it before writing.")
            connection.execute("DELETE FROM ledger_rules WHERE id=?", (rule_id,))
            self._audit(connection, "rule", rule_id, "delete", dict(rule), None)

    def classify(
        self,
        transaction_id: str,
        profile_code: str,
        category_code: str | None,
        source: str,
        confidence: float,
        rationale: str,
        status: str = "applied",
        expected_version: int | None = None,
        *,
        model_version: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        profile_id = self.ensure_profile(profile_code, profile_code)
        category_id = (
            self.ensure_category(category_code, category_code.replace(".", " ").title())
            if category_code
            else None
        )
        with self.database.connect() as connection:
            if category_id is not None:
                profile_categories = connection.execute(
                    "SELECT count(*) FROM ledger_profile_categories WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()[0]
                category_allowed = connection.execute(
                    """SELECT 1 FROM ledger_profile_categories
                       WHERE profile_id=? AND category_id=?""",
                    (profile_id, category_id),
                ).fetchone()
                if profile_categories and not category_allowed:
                    raise ValueError(
                        f"Category {category_code} does not belong to profile {profile_code}"
                    )
                if not profile_categories:
                    connection.execute(
                        """INSERT OR IGNORE INTO ledger_profile_categories(
                               profile_id, category_id) VALUES (?, ?)""",
                        (profile_id, category_id),
                    )
            transaction = connection.execute(
                "SELECT version FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if transaction is None:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if expected_version is not None and transaction["version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            identifier = self._id("classification")
            connection.execute(
                """INSERT INTO ledger_classifications(
                       id, transaction_id, profile_id, category_id, source,
                       confidence, rationale, status, model_version,
                       prompt_hash, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    transaction_id,
                    profile_id,
                    category_id,
                    source,
                    confidence,
                    rationale,
                    status,
                    model_version,
                    prompt_hash,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            connection.execute(
                "UPDATE ledger_transactions SET version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "classify",
                {"version": transaction["version"]},
                {
                    "category": category_code,
                    "source": source,
                    "status": status,
                    "model_version": model_version,
                    "prompt_hash": prompt_hash,
                    "metadata": metadata or {},
                },
            )
            return identifier

    def list_classifications(
        self,
        *,
        status: str | None = None,
        profile_code: str | None = None,
        transaction_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if status:
            clauses.append("cl.status=?")
            parameters.append(status)
        if profile_code:
            clauses.append("p.code=?")
            parameters.append(profile_code)
        if transaction_id:
            clauses.append("cl.transaction_id=?")
            parameters.append(transaction_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT cl.*, p.code AS profile_code, c.code AS category_code,
                              t.version AS transaction_version, t.booking_date,
                              t.amount_minor, t.currency, t.purpose
                       FROM ledger_classifications cl
                       JOIN ledger_profiles p ON p.id=cl.profile_id
                       LEFT JOIN ledger_categories c ON c.id=cl.category_id
                       JOIN ledger_transactions t ON t.id=cl.transaction_id"""
                    + where
                    + " ORDER BY cl.created_at DESC, cl.id DESC LIMIT ?",
                    parameters,
                )
            ]

    def review_classification(
        self,
        classification_id: str,
        decision: str,
        expected_version: int,
    ) -> dict:
        if decision not in {"accept", "reject"}:
            raise ValueError("Decision must be accept or reject")
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT cl.*, t.version AS transaction_version
                   FROM ledger_classifications cl
                   JOIN ledger_transactions t ON t.id=cl.transaction_id
                   WHERE cl.id=?""",
                (classification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown classification {classification_id}")
            if row["status"] != "suggested":
                raise ValueError("Only suggested classifications can be reviewed")
            if row["transaction_version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            status = "applied" if decision == "accept" else "rejected"
            connection.execute(
                """UPDATE ledger_classifications
                   SET status=?, version=version+1 WHERE id=?""",
                (status, classification_id),
            )
            connection.execute(
                """UPDATE ledger_transactions SET version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["transaction_id"],),
            )
            self._audit(
                connection,
                "classification",
                classification_id,
                decision,
                {"status": "suggested", "version": row["version"]},
                {"status": status, "version": row["version"] + 1},
            )
            return {
                "classification_id": classification_id,
                "status": status,
                "transaction_version": expected_version + 1,
            }

    def apply_rules(self, transaction_id: str, profile_code: str) -> str | None:
        from .classification import ClassificationService

        transaction = self.get_transaction(transaction_id)
        service = ClassificationService(self)
        if not service.profile_available(
            transaction["account_id"], profile_code
        ) or not service.stage_enabled_for_account(transaction["account_id"], "rule"):
            return None
        result = service.find_rule(transaction, profile_code)
        if result is None:
            return None
        return self.classify(
            transaction_id,
            profile_code,
            result.category_code,
            result.stage,
            result.confidence,
            result.rationale,
            expected_version=transaction["version"],
            metadata={"source_id": result.source_id, **result.detail},
        )

    def split_transaction(
        self,
        transaction_id: str,
        profile_code: str,
        parts: list[tuple[object, str | None, str | None]],
        expected_version: int,
    ) -> list[str]:
        """Replace one displayed booking by balanced reportable split rows."""
        from .models import to_minor_units

        prepared_parts = [
            (
                to_minor_units(amount if isinstance(amount, Decimal) else Decimal(str(amount))),
                self.ensure_category(category_code, category_code.replace(".", " ").title())
                if category_code
                else None,
                memo,
            )
            for amount, category_code, memo in parts
        ]
        with self.database.connect() as connection:
            transaction = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if not transaction:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if transaction["version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            if transaction["state"] != "active":
                raise ValueError("Only active transactions can be split.")
            if transaction["booking_status"] != TransactionStatus.BOOKED.value:
                raise ValueError("Only booked transactions can be split.")
            amounts = [amount for amount, _, _ in prepared_parts]
            if (
                not parts
                or any(amount == 0 for amount in amounts)
                or sum(amounts) != transaction["amount_minor"]
            ):
                raise ValueError(
                    "Split amounts must be non-zero and sum exactly to the original transaction."
                )
            identifiers: list[str] = []
            for amount_minor, category_id, memo in prepared_parts:
                identifier = self._id("split")
                connection.execute(
                    "INSERT INTO ledger_splits(id, transaction_id, amount_minor, category_id, memo) VALUES (?, ?, ?, ?, ?)",
                    (identifier, transaction_id, amount_minor, category_id, memo),
                )
                identifiers.append(identifier)
            connection.execute(
                "UPDATE ledger_transactions SET state='split_parent', version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "split",
                {"amount_minor": transaction["amount_minor"]},
                {"parts": len(parts), "profile": profile_code},
            )
            return identifiers

    def revert_split(self, transaction_id: str, expected_version: int) -> None:
        with self.database.connect() as connection:
            transaction = connection.execute(
                "SELECT state, version FROM ledger_transactions WHERE id=?",
                (transaction_id,),
            ).fetchone()
            if transaction is None:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if transaction["version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            if transaction["state"] != "split_parent":
                raise ValueError("Transaction is not split")
            parts = connection.execute(
                "SELECT count(*) FROM ledger_splits WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM ledger_splits WHERE transaction_id=?",
                (transaction_id,),
            )
            connection.execute(
                """UPDATE ledger_transactions SET state='active',
                   version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "split_revert",
                {"parts": parts, "version": expected_version},
                {"state": "active", "version": expected_version + 1},
            )

    def replace_split(
        self,
        transaction_id: str,
        profile_code: str,
        parts: list[tuple[object, str | None, str | None]],
        expected_version: int,
    ) -> list[str]:
        from .models import to_minor_units

        prepared = [
            (
                to_minor_units(amount if isinstance(amount, Decimal) else Decimal(str(amount))),
                self.ensure_category(
                    category_code,
                    category_code.replace(".", " ").title(),
                )
                if category_code
                else None,
                memo,
            )
            for amount, category_code, memo in parts
        ]
        with self.database.connect() as connection:
            transaction = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if transaction is None:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if transaction["version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            if transaction["state"] != "split_parent":
                raise ValueError("Transaction is not split")
            if (
                not prepared
                or any(part[0] == 0 for part in prepared)
                or sum(part[0] for part in prepared) != transaction["amount_minor"]
            ):
                raise ValueError(
                    "Split amounts must be non-zero and sum exactly to the original transaction."
                )
            previous = connection.execute(
                "SELECT count(*) FROM ledger_splits WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM ledger_splits WHERE transaction_id=?",
                (transaction_id,),
            )
            identifiers = []
            for amount_minor, category_id, memo in prepared:
                identifier = self._id("split")
                connection.execute(
                    """INSERT INTO ledger_splits(
                           id, transaction_id, amount_minor, category_id, memo
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (identifier, transaction_id, amount_minor, category_id, memo),
                )
                identifiers.append(identifier)
            connection.execute(
                """UPDATE ledger_transactions SET version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "split_replace",
                {"parts": previous, "version": expected_version},
                {"parts": len(prepared), "version": expected_version + 1, "profile": profile_code},
            )
            return identifiers

    def sync_split_metadata(
        self,
        transaction_id: str,
        parts: list[tuple[object, str | None, dict[str, object]]],
        expected_version: int,
    ) -> int:
        """Synchronize metadata for an unchanged, uniquely identifiable split."""
        from .models import to_minor_units

        prepared = [
            (
                to_minor_units(amount if isinstance(amount, Decimal) else Decimal(str(amount))),
                self.ensure_category(
                    category_code,
                    category_code.replace(".", " ").title(),
                )
                if category_code
                else None,
                json.dumps(metadata, sort_keys=True),
            )
            for amount, category_code, metadata in parts
        ]
        if not prepared:
            raise ValueError("Split metadata requires at least one part")
        with self.database.connect() as connection:
            transaction = connection.execute(
                "SELECT state, version FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if transaction is None:
                raise KeyError(f"Unknown transaction {transaction_id}")
            if transaction["version"] != expected_version:
                raise VersionConflictError("The transaction has changed; reload it before writing.")
            if transaction["state"] != "split_parent":
                raise ValueError("Transaction is not split")
            existing = connection.execute(
                """SELECT id, amount_minor, category_id, metadata_json
                     FROM ledger_splits WHERE transaction_id=?""",
                (transaction_id,),
            ).fetchall()
            expected_by_key: dict[tuple[int, str | None], set[str]] = {}
            for amount_minor, category_id, metadata_json in prepared:
                expected_by_key.setdefault((amount_minor, category_id), set()).add(metadata_json)
            if any(len(metadata_values) > 1 for metadata_values in expected_by_key.values()):
                raise ValueError("Split metadata contains ambiguous duplicate parts")
            existing_by_key: dict[tuple[int, str | None], list[sqlite3.Row]] = {}
            for row in existing:
                existing_by_key.setdefault((row["amount_minor"], row["category_id"]), []).append(
                    row
                )
            if set(existing_by_key) != set(expected_by_key) or any(
                len(existing_by_key[key])
                != sum(1 for amount, category, _ in prepared if (amount, category) == key)
                for key in expected_by_key
            ):
                raise ValueError("Split metadata does not match the current split structure")
            updates: list[tuple[str, str]] = []
            for key, expected_metadata in expected_by_key.items():
                metadata_json = next(iter(expected_metadata))
                updates.extend(
                    (metadata_json, row["id"])
                    for row in existing_by_key[key]
                    if json.loads(row["metadata_json"] or "{}") != json.loads(metadata_json)
                )
            if not updates:
                return 0
            connection.executemany("UPDATE ledger_splits SET metadata_json=? WHERE id=?", updates)
            connection.execute(
                """UPDATE ledger_transactions SET version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transaction_id,),
            )
            self._audit(
                connection,
                "transaction",
                transaction_id,
                "split_metadata_sync",
                {"version": expected_version},
                {"parts": len(updates), "version": expected_version + 1},
            )
            return len(updates)

    def add_hint(
        self,
        profile_code: str,
        text: str,
        *,
        pattern: str | None = None,
        category_code: str | None = None,
        priority: int = 100,
        account_id: str | None = None,
        active: bool = True,
    ) -> str:
        if bool(pattern) != bool(category_code):
            raise ValueError("Deterministic hints require both pattern and category")
        if pattern:
            self._validate_pattern(pattern)
        identifier = self._id("hint")
        with self.database.connect() as connection:
            profile = connection.execute(
                "SELECT * FROM ledger_profiles WHERE code=?", (profile_code,)
            ).fetchone()
            if profile is None:
                raise KeyError(f"Unknown profile {profile_code}")
            category_id = None
            if category_code:
                _profile, category = self._rule_targets(
                    connection, profile_code, category_code, account_id
                )
                category_id = category["id"]
            elif (
                account_id is not None
                and not connection.execute(
                    "SELECT 1 FROM ledger_accounts WHERE id=?", (account_id,)
                ).fetchone()
            ):
                raise KeyError(f"Unknown account {account_id}")
            connection.execute(
                """INSERT INTO ledger_hints(
                       id, profile_id, text, pattern, category_id, priority, account_id, active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    profile["id"],
                    text.strip(),
                    pattern,
                    category_id,
                    priority,
                    account_id,
                    int(active),
                ),
            )
            self._audit(
                connection,
                "hint",
                identifier,
                "create",
                None,
                {
                    "profile": profile_code,
                    "pattern": pattern,
                    "category": category_code,
                    "account_id": account_id,
                },
            )
        return identifier

    def list_hints(
        self,
        *,
        profile_code: str | None = None,
        account_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[dict]:
        query = """SELECT h.*, p.code AS profile_code, c.code AS category_code,
                          a.display_name AS account_display_name
                   FROM ledger_hints h JOIN ledger_profiles p ON p.id=h.profile_id
                   LEFT JOIN ledger_categories c ON c.id=h.category_id
                   LEFT JOIN ledger_accounts a ON a.id=h.account_id WHERE 1=1"""
        parameters: list[object] = []
        if profile_code:
            query += " AND p.code=?"
            parameters.append(profile_code)
        if account_id:
            query += " AND (h.account_id IS NULL OR h.account_id=?)"
            parameters.append(account_id)
        if not include_inactive:
            query += " AND h.active=1"
        query += " ORDER BY p.code, h.priority, h.created_at, h.id"
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def update_hint(
        self,
        hint_id: str,
        *,
        expected_version: int,
        text: str | None = None,
        pattern: str | None = None,
        category_code: str | None = None,
        priority: int | None = None,
        active: bool | None = None,
        account_id: object = _UNSET,
    ) -> dict:
        with self.database.connect() as connection:
            hint = connection.execute(
                """SELECT h.*, p.code AS profile_code, c.code AS category_code FROM ledger_hints h
                   JOIN ledger_profiles p ON p.id=h.profile_id LEFT JOIN ledger_categories c ON c.id=h.category_id WHERE h.id=?""",
                (hint_id,),
            ).fetchone()
            if hint is None:
                raise KeyError(f"Unknown hint {hint_id}")
            if hint["version"] != expected_version:
                raise VersionConflictError("The hint has changed; reload it before writing.")
            next_pattern = pattern if pattern is not None else hint["pattern"]
            next_category = category_code if category_code is not None else hint["category_code"]
            if bool(next_pattern) != bool(next_category):
                raise ValueError("Deterministic hints require both pattern and category")
            category_id = None
            next_account_id = cast(
                str | None, hint["account_id"] if account_id is _UNSET else account_id
            )
            if next_pattern:
                self._validate_pattern(next_pattern)
                _profile, category = self._rule_targets(
                    connection, hint["profile_code"], next_category, next_account_id
                )
                category_id = category["id"]
            elif (
                next_account_id is not None
                and not connection.execute(
                    "SELECT 1 FROM ledger_accounts WHERE id=?", (next_account_id,)
                ).fetchone()
            ):
                raise KeyError(f"Unknown account {next_account_id}")
            values = (
                text.strip() if text is not None else hint["text"],
                next_pattern,
                category_id,
                priority if priority is not None else hint["priority"],
                int(active) if active is not None else hint["active"],
                next_account_id,
            )
            if values == (
                hint["text"],
                hint["pattern"],
                hint["category_id"],
                hint["priority"],
                hint["active"],
                hint["account_id"],
            ):
                return {**dict(hint), "changed": False}
            connection.execute(
                """UPDATE ledger_hints SET text=?, pattern=?, category_id=?, priority=?, active=?, account_id=?, version=version+1 WHERE id=?""",
                (*values, hint_id),
            )
            result = {
                **dict(hint),
                "text": values[0],
                "pattern": values[1],
                "category_id": values[2],
                "category_code": next_category,
                "priority": values[3],
                "active": values[4],
                "account_id": values[5],
                "version": expected_version + 1,
                "changed": True,
            }
            self._audit(connection, "hint", hint_id, "update", dict(hint), result)
            return result

    def delete_hint(self, hint_id: str, expected_version: int) -> None:
        with self.database.connect() as connection:
            hint = connection.execute(
                "SELECT * FROM ledger_hints WHERE id=?", (hint_id,)
            ).fetchone()
            if hint is None:
                raise KeyError(f"Unknown hint {hint_id}")
            if hint["version"] != expected_version:
                raise VersionConflictError("The hint has changed; reload it before writing.")
            connection.execute("DELETE FROM ledger_hints WHERE id=?", (hint_id,))
            self._audit(connection, "hint", hint_id, "delete", dict(hint), None)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        operation: str,
        before: object,
        after: object,
        *,
        actor: str = "system",
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO ledger_audit_events(
                   entity_type, entity_id, operation, before_json, after_json,
                   actor, correlation_id, idempotency_key
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type,
                entity_id,
                operation,
                json.dumps(before, sort_keys=True, default=str),
                json.dumps(after, sort_keys=True, default=str),
                actor,
                correlation_id,
                idempotency_key,
            ),
        )

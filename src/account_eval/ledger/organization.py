"""Personal organization, search, review, and safe bulk workflows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from account_eval.accounts import AccountScope

from .models import (
    VALID_TRANSACTION_STATUS_TRANSITIONS,
    AccountType,
    BulkAction,
    TransactionSearchFilter,
    account_type,
    to_minor_units,
    transaction_status,
)
from .repository import LedgerRepository, VersionConflictError
from .transaction_query import SEARCH_COLUMNS, build_transaction_query

REVIEW_ITEM_TYPES = frozenset(
    {
        "new_transaction",
        "pending_transaction",
        "classification_suggestion",
        "relation_suggestion",
        "document_match",
        "unmatched_document",
        "reconciliation_difference",
        "duplicate_candidate",
        "import_warning",
        "processing_error",
    }
)


def _restricts(allowed: set[str], group: str) -> bool:
    """A whitelist only narrows the group (profile or dimension) it names."""
    return any(code.startswith(f"{group}:") for code in allowed)


def normalize_merchant_alias(value: str) -> str:
    """Normalize comparison text without hiding the original display value."""
    return " ".join(value.casefold().strip().split())


class OrganizationService:
    """One transaction-safe service shared by CLI and MCP."""

    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository

    @staticmethod
    def _key(value: str, namespace: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("idempotency_key must contain at least 8 characters")
        return hashlib.sha256(f"{namespace}|{normalized}".encode()).hexdigest()

    @staticmethod
    def _clean_name(value: str, label: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError(f"{label} must not be empty")
        return cleaned

    @staticmethod
    def _decode_record(row: dict[str, Any]) -> dict[str, Any]:
        for key, fallback in (
            ("tags_json", []),
            ("dimensions_json", {}),
            ("metadata_json", {}),
        ):
            if key in row and isinstance(row[key], str):
                try:
                    row[key.removesuffix("_json")] = json.loads(row[key])
                except json.JSONDecodeError:
                    row[key.removesuffix("_json")] = fallback
                del row[key]
        return row

    def create_manual_account(
        self,
        display_name: str,
        *,
        account_kind: str | AccountType,
        currency: str = "EUR",
        idempotency_key: str,
        opening_balance: Decimal | None = None,
        balance_date: date | None = None,
    ) -> dict[str, Any]:
        name = self._clean_name(display_name, "Account name")
        kind = account_type(account_kind).value
        normalized_currency = currency.strip().upper()
        if len(normalized_currency) != 3:
            raise ValueError("Currency must be an ISO 4217 three-letter code")
        digest = self._key(idempotency_key, "manual-account")
        external_id = f"manual-account-{digest[:24]}"
        provider_id = self.repository.ensure_provider("manual", "Manuell")
        opening_minor = to_minor_units(opening_balance) if opening_balance is not None else None
        effective_date = balance_date or date.today()
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                """SELECT * FROM ledger_accounts
                   WHERE provider_id=? AND external_id=?""",
                (provider_id, external_id),
            ).fetchone()
            if existing:
                expected = (
                    name,
                    normalized_currency,
                    kind,
                    opening_minor,
                    effective_date.isoformat() if opening_minor is not None else None,
                )
                actual = (
                    existing["display_name"],
                    existing["currency"],
                    existing["account_type"],
                    existing["last_balance_minor"],
                    existing["last_balance_at"],
                )
                if actual != expected:
                    raise ValueError("idempotency_key was already used with different account data")
                return {**dict(existing), "created": False}
            identifier = self.repository._id("account")
            connection.execute(
                """INSERT INTO ledger_accounts(
                       id, provider_id, external_id, display_name, currency,
                       account_type, last_balance_minor, last_balance_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    provider_id,
                    external_id,
                    name,
                    normalized_currency,
                    kind,
                    opening_minor,
                    effective_date.isoformat() if opening_minor is not None else None,
                ),
            )
            if opening_minor is not None:
                snapshot_id = self.repository._id("balance")
                connection.execute(
                    """INSERT INTO ledger_balance_snapshots(
                           id, account_id, balance_date, balance_minor,
                           currency, source, external_id, idempotency_key,
                           metadata_json
                       ) VALUES (?, ?, ?, ?, ?, 'manual-opening', ?, ?, '{}')""",
                    (
                        snapshot_id,
                        identifier,
                        effective_date.isoformat(),
                        opening_minor,
                        normalized_currency,
                        external_id,
                        f"manual-opening:{digest}",
                    ),
                )
                connection.execute(
                    """INSERT INTO ledger_reconciliations(
                           id, account_id, snapshot_id, transaction_count, status
                       ) VALUES (?, ?, ?, 0, 'baseline')""",
                    (self.repository._id("reconciliation"), identifier, snapshot_id),
                )
            self.repository._audit(
                connection,
                "account",
                identifier,
                "manual_create",
                None,
                {
                    "display_name": name,
                    "currency": normalized_currency,
                    "account_type": kind,
                    "opening_balance_minor": opening_minor,
                },
                actor="manual",
                idempotency_key=f"manual-account:{digest}",
            )
            created = connection.execute(
                "SELECT * FROM ledger_accounts WHERE id=?", (identifier,)
            ).fetchone()
            return {**dict(created), "created": True}

    def create_manual_transaction(
        self,
        account_id: str,
        *,
        booking_date: date,
        amount: Decimal,
        purpose: str,
        idempotency_key: str,
        valuta_date: date | None = None,
        counterparty: str | None = None,
        reference: str | None = None,
        booking_status: str = "booked",
        category_profile: str | None = None,
        category_code: str | None = None,
        tags: tuple[str, ...] = (),
        dimensions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        cleaned_purpose = self._clean_name(purpose, "Purpose")
        status = transaction_status(booking_status).value
        amount_minor = to_minor_units(amount)
        if amount_minor == 0:
            raise ValueError("Manual transactions must have a non-zero amount")
        digest = self._key(idempotency_key, "manual-transaction")
        external_id = f"manual-transaction-{digest[:24]}"
        with self.repository.database.connect() as connection:
            account = connection.execute(
                """SELECT a.*, p.kind AS provider_kind
                   FROM ledger_accounts a
                   JOIN ledger_providers p ON p.id=a.provider_id
                   WHERE a.id=?""",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {account_id}")
            if not account["enabled"]:
                raise ValueError("Manual transaction account is disabled")
            if account["provider_kind"] != "manual":
                raise ValueError(
                    "Manual transactions are only allowed on manual accounts; "
                    "use import preview or provider sync for bank accounts"
                )
            existing = connection.execute(
                """SELECT t.* FROM ledger_source_transactions st
                   JOIN ledger_transactions t ON t.source_transaction_id=st.id
                   WHERE st.provider_id=? AND st.account_id=?
                     AND st.external_id=?""",
                (account["provider_id"], account_id, external_id),
            ).fetchone()
            expected = {
                "account_id": account_id,
                "booking_date": booking_date.isoformat(),
                "valuta_date": valuta_date.isoformat() if valuta_date else None,
                "amount_minor": amount_minor,
                "currency": account["currency"],
                "purpose": cleaned_purpose,
                "counterparty": counterparty,
                "reference": reference,
                "booking_status": status,
            }
            if existing:
                actual = {key: existing[key] for key in expected}
                if actual != expected:
                    raise ValueError(
                        "idempotency_key was already used with different transaction data"
                    )
                return {**dict(existing), "created": False}
            merchant_id = self._merchant_for_text(connection, counterparty or cleaned_purpose)
            source_id = self.repository._id("source")
            transaction_id = self.repository._id("transaction")
            raw = json.dumps(
                {"origin": "manual", **expected},
                ensure_ascii=False,
                sort_keys=True,
            )
            connection.execute(
                """INSERT INTO ledger_source_transactions(
                       id, provider_id, account_id, external_id,
                       idempotency_key, raw_payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    account["provider_id"],
                    account_id,
                    external_id,
                    f"manual-source:{digest}",
                    raw,
                ),
            )
            connection.execute(
                """INSERT INTO ledger_source_transaction_revisions(
                       id, source_transaction_id, idempotency_key,
                       raw_payload_json, normalized_payload_json, booking_status
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self.repository._id("source-revision"),
                    source_id,
                    f"manual-revision:{digest}",
                    raw,
                    json.dumps(expected, ensure_ascii=False, sort_keys=True),
                    status,
                ),
            )
            connection.execute(
                """INSERT INTO ledger_transactions(
                       id, source_transaction_id, account_id, booking_date,
                       valuta_date, amount_minor, currency, purpose,
                       counterparty, reference, merchant_id, booking_status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    transaction_id,
                    source_id,
                    account_id,
                    expected["booking_date"],
                    expected["valuta_date"],
                    amount_minor,
                    account["currency"],
                    cleaned_purpose,
                    counterparty,
                    reference,
                    merchant_id,
                    status,
                ),
            )
            connection.execute(
                """INSERT INTO ledger_transaction_status_events(
                       id, transaction_id, from_status, to_status,
                       transaction_version, reason, source
                   ) VALUES (?, ?, NULL, ?, 1,
                             'Manually entered transaction', 'manual')""",
                (self.repository._id("status-event"), transaction_id, status),
            )
            self._enqueue_review(
                connection,
                "new_transaction",
                "transaction",
                transaction_id,
                "Review manually entered transaction",
                60,
            )
            if status == "pending":
                self._enqueue_review(
                    connection,
                    "pending_transaction",
                    "transaction",
                    transaction_id,
                    "Review pending manual transaction",
                    80,
                )
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                "manual_create",
                None,
                expected | {"merchant_id": merchant_id},
                actor="manual",
                idempotency_key=f"manual-transaction:{digest}",
            )
            created = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            result = {**dict(created), "created": True}
        version = int(result["version"])
        self._apply_transaction_organization(
            result["id"],
            version,
            category_profile=category_profile,
            category_code=category_code,
            tags=tags,
            dimensions=dimensions,
        )
        refreshed = self.repository.database.connect()
        with refreshed as connection:
            row = connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?",
                (result["id"],),
            ).fetchone()
        return {**dict(row), "created": result["created"]}

    def _apply_transaction_organization(
        self,
        transaction_id: str,
        version: int,
        *,
        category_profile: str | None = None,
        category_code: str | None = None,
        tags: tuple[str, ...] = (),
        dimensions: dict[str, str] | None = None,
    ) -> int:
        current_version = version
        if category_profile and category_code:
            self.repository.classify(
                transaction_id,
                category_profile,
                category_code,
                source="manual",
                confidence=1.0,
                rationale="Manual entry",
                status="applied",
                expected_version=current_version,
            )
            current_version += 1
        for dimension_code, value_code in (dimensions or {}).items():
            self._ensure_dimension_value(dimension_code, value_code)
            result = self.set_transaction_dimension(
                transaction_id,
                dimension_code,
                value_code=value_code,
                expected_version=current_version,
            )
            current_version = int(result["version"])
        for tag_name in tags:
            self.create_tag(tag_name)
            result = self.tag_transaction(
                transaction_id,
                tag_name,
                expected_version=current_version,
            )
            current_version = int(result["version"])
        return current_version

    def _ensure_dimension_value(self, dimension_code: str, value_code: str) -> None:
        dimensions = self.list_dimensions()
        if not any(item["code"] == dimension_code for item in dimensions):
            self.create_dimension(
                dimension_code,
                dimension_code.replace("-", " ").title(),
            )
            dimensions = self.list_dimensions()
        values: list[dict[str, Any]] = next(
            (item["values"] for item in dimensions if item["code"] == dimension_code),
            [],
        )
        if not any(value["code"] == value_code for value in values):
            self.create_dimension_value(
                dimension_code,
                value_code,
                value_code.replace("-", " ").title(),
            )

    @staticmethod
    def _merchant_for_text(connection: sqlite3.Connection, value: str | None) -> str | None:
        if not value:
            return None
        normalized = normalize_merchant_alias(value)
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
            (" ".join(value.strip().split()),),
        ).fetchone()
        return row["id"] if row else None

    def create_tag(self, name: str) -> dict[str, Any]:
        normalized = self._clean_name(name, "Tag name")
        with self.repository.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ledger_tags WHERE name=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if row:
                return {**dict(row), "created": False}
            identifier = self.repository._id("tag")
            connection.execute(
                "INSERT INTO ledger_tags(id, name) VALUES (?, ?)",
                (identifier, normalized),
            )
            self.repository._audit(
                connection, "tag", identifier, "create", None, {"name": normalized}
            )
            return {
                **dict(
                    connection.execute(
                        "SELECT * FROM ledger_tags WHERE id=?", (identifier,)
                    ).fetchone()
                ),
                "created": True,
            }

    def list_tags(self) -> list[dict[str, Any]]:
        with self.repository.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT tag.*, COUNT(tt.transaction_id) AS transaction_count
                       FROM ledger_tags tag
                       LEFT JOIN ledger_transaction_tags tt ON tt.tag_id=tag.id
                       GROUP BY tag.id ORDER BY tag.name COLLATE NOCASE"""
                )
            ]

    def tag_transaction(
        self,
        transaction_id: str,
        tag_name_or_id: str,
        *,
        expected_version: int,
        remove: bool = False,
    ) -> dict[str, Any]:
        operation = "tag_remove" if remove else "tag_add"
        with self.repository.database.connect() as connection:
            transaction = self._versioned_transaction(connection, transaction_id, expected_version)
            tag = connection.execute(
                """SELECT * FROM ledger_tags
                   WHERE id=? OR name=? COLLATE NOCASE""",
                (tag_name_or_id, tag_name_or_id),
            ).fetchone()
            if tag is None:
                raise KeyError(f"Unknown tag {tag_name_or_id}")
            exists = connection.execute(
                """SELECT 1 FROM ledger_transaction_tags
                   WHERE transaction_id=? AND tag_id=?""",
                (transaction_id, tag["id"]),
            ).fetchone()
            if bool(exists) == (not remove):
                return {
                    "transaction_id": transaction_id,
                    "tag": tag["name"],
                    "version": expected_version,
                    "changed": False,
                }
            if remove:
                connection.execute(
                    """DELETE FROM ledger_transaction_tags
                       WHERE transaction_id=? AND tag_id=?""",
                    (transaction_id, tag["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO ledger_transaction_tags(transaction_id, tag_id)
                       VALUES (?, ?)""",
                    (transaction_id, tag["id"]),
                )
            version = self._touch_transaction(connection, transaction_id)
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                operation,
                {"version": transaction["version"]},
                {"tag_id": tag["id"], "tag": tag["name"], "version": version},
            )
            return {
                "transaction_id": transaction_id,
                "tag": tag["name"],
                "version": version,
                "changed": True,
            }

    def create_dimension(self, code: str, name: str) -> dict[str, Any]:
        normalized_code = self._code(code, "Dimension code")
        normalized_name = self._clean_name(name, "Dimension name")
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE",
                (normalized_code,),
            ).fetchone()
            if existing:
                if existing["name"] != normalized_name:
                    raise ValueError("Dimension code already has a different name")
                return {**dict(existing), "created": False}
            identifier = self.repository._id("dimension")
            connection.execute(
                "INSERT INTO ledger_dimensions(id, code, name) VALUES (?, ?, ?)",
                (identifier, normalized_code, normalized_name),
            )
            self.repository._audit(
                connection,
                "dimension",
                identifier,
                "create",
                None,
                {"code": normalized_code, "name": normalized_name},
            )
            return {
                "id": identifier,
                "code": normalized_code,
                "name": normalized_name,
                "version": 1,
                "created": True,
            }

    def create_dimension_value(self, dimension_code: str, code: str, name: str) -> dict[str, Any]:
        normalized_code = self._code(code, "Dimension value code")
        normalized_name = self._clean_name(name, "Dimension value name")
        with self.repository.database.connect() as connection:
            dimension = connection.execute(
                "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE",
                (dimension_code,),
            ).fetchone()
            if dimension is None:
                raise KeyError(f"Unknown dimension {dimension_code}")
            existing = connection.execute(
                """SELECT * FROM ledger_dimension_values
                   WHERE dimension_id=? AND code=? COLLATE NOCASE""",
                (dimension["id"], normalized_code),
            ).fetchone()
            if existing:
                if existing["name"] != normalized_name:
                    raise ValueError("Dimension value code already has a different name")
                return {**dict(existing), "created": False}
            identifier = self.repository._id("dimension-value")
            connection.execute(
                """INSERT INTO ledger_dimension_values(
                       id, dimension_id, code, name
                   ) VALUES (?, ?, ?, ?)""",
                (identifier, dimension["id"], normalized_code, normalized_name),
            )
            self.repository._audit(
                connection,
                "dimension_value",
                identifier,
                "create",
                None,
                {"dimension": dimension["code"], "code": normalized_code, "name": normalized_name},
            )
            return {
                "id": identifier,
                "dimension_code": dimension["code"],
                "code": normalized_code,
                "name": normalized_name,
                "version": 1,
                "created": True,
            }

    @staticmethod
    def _code(value: str, label: str) -> str:
        code = value.strip().casefold().replace(" ", "-")
        if not code or not all(character.isalnum() or character in "-_." for character in code):
            raise ValueError(f"{label} may only contain letters, numbers, -, _ and .")
        return code

    def list_dimensions(self) -> list[dict[str, Any]]:
        with self.repository.database.connect() as connection:
            dimensions = [
                dict(row)
                for row in connection.execute("SELECT * FROM ledger_dimensions ORDER BY code")
            ]
            for dimension in dimensions:
                dimension["values"] = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT id, code, name, active, version, created_at, updated_at
                           FROM ledger_dimension_values
                           WHERE dimension_id=? ORDER BY code""",
                        (dimension["id"],),
                    )
                ]
            return dimensions

    def update_dimension(
        self,
        code: str,
        *,
        name: str | None = None,
        active: bool | None = None,
        expected_version: int,
    ) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            dimension = connection.execute(
                "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE", (code,)
            ).fetchone()
            if dimension is None:
                raise KeyError(f"Unknown dimension {code}")
            if dimension["version"] != expected_version:
                raise VersionConflictError("The dimension has changed; reload it before writing.")
            changes = {
                "name": self._clean_name(name, "Dimension name")
                if name is not None
                else dimension["name"],
                "active": int(active) if active is not None else dimension["active"],
            }
            if changes["name"] == dimension["name"] and changes["active"] == dimension["active"]:
                return {**dict(dimension), "changed": False}
            connection.execute(
                """UPDATE ledger_dimensions SET name=?, active=?, version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (changes["name"], changes["active"], dimension["id"]),
            )
            result = {
                **dict(dimension),
                **changes,
                "version": dimension["version"] + 1,
                "changed": True,
            }
            self.repository._audit(
                connection, "dimension", dimension["id"], "update", dict(dimension), result
            )
            return result

    def update_dimension_value(
        self,
        dimension_code: str,
        value_code: str,
        *,
        name: str | None = None,
        active: bool | None = None,
        expected_version: int,
    ) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            value = connection.execute(
                """SELECT dv.*, d.code AS dimension_code FROM ledger_dimension_values dv
                   JOIN ledger_dimensions d ON d.id=dv.dimension_id
                   WHERE d.code=? COLLATE NOCASE AND dv.code=? COLLATE NOCASE""",
                (dimension_code, value_code),
            ).fetchone()
            if value is None:
                raise KeyError(f"Unknown value {value_code} for dimension {dimension_code}")
            if value["version"] != expected_version:
                raise VersionConflictError(
                    "The dimension value has changed; reload it before writing."
                )
            changes = {
                "name": self._clean_name(name, "Dimension value name")
                if name is not None
                else value["name"],
                "active": int(active) if active is not None else value["active"],
            }
            if changes["name"] == value["name"] and changes["active"] == value["active"]:
                return {**dict(value), "changed": False}
            connection.execute(
                """UPDATE ledger_dimension_values SET name=?, active=?, version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (changes["name"], changes["active"], value["id"]),
            )
            result = {**dict(value), **changes, "version": value["version"] + 1, "changed": True}
            self.repository._audit(
                connection, "dimension_value", value["id"], "update", dict(value), result
            )
            return result

    def delete_dimension(self, code: str) -> None:
        with self.repository.database.connect() as connection:
            dimension = connection.execute(
                "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE", (code,)
            ).fetchone()
            if dimension is None:
                raise KeyError(f"Unknown dimension {code}")
            uses = connection.execute(
                "SELECT count(*) FROM ledger_transaction_dimensions WHERE dimension_id=?",
                (dimension["id"],),
            ).fetchone()[0]
            if uses:
                raise ValueError(
                    f"Dimension is used by {uses} transaction(s); deactivate it instead"
                )
            connection.execute("DELETE FROM ledger_dimensions WHERE id=?", (dimension["id"],))
            self.repository._audit(
                connection, "dimension", dimension["id"], "delete", dict(dimension), None
            )

    def delete_dimension_value(self, dimension_code: str, value_code: str) -> None:
        with self.repository.database.connect() as connection:
            value = connection.execute(
                """SELECT dv.*, d.code AS dimension_code FROM ledger_dimension_values dv
                   JOIN ledger_dimensions d ON d.id=dv.dimension_id
                   WHERE d.code=? COLLATE NOCASE AND dv.code=? COLLATE NOCASE""",
                (dimension_code, value_code),
            ).fetchone()
            if value is None:
                raise KeyError(f"Unknown value {value_code} for dimension {dimension_code}")
            uses = connection.execute(
                "SELECT count(*) FROM ledger_transaction_dimensions WHERE dimension_value_id=?",
                (value["id"],),
            ).fetchone()[0]
            if uses:
                raise ValueError(
                    f"Dimension value is used by {uses} transaction(s); deactivate it instead"
                )
            connection.execute("DELETE FROM ledger_dimension_values WHERE id=?", (value["id"],))
            self.repository._audit(
                connection, "dimension_value", value["id"], "delete", dict(value), None
            )

    def list_account_columns(self, account_id: str) -> list[dict[str, Any]]:
        with self.repository.database.connect() as connection:
            self.repository.get_account(account_id)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT d.code, d.name, d.active, ac.position FROM ledger_account_columns ac
                       JOIN ledger_dimensions d ON d.id=ac.dimension_id
                       WHERE ac.account_id=? ORDER BY ac.position""",
                    (account_id,),
                )
            ]

    def set_account_columns(
        self, account_id: str, dimension_codes: list[str]
    ) -> list[dict[str, Any]]:
        if len(set(dimension_codes)) != len(dimension_codes):
            raise ValueError("Dimension codes must be unique")
        with self.repository.database.connect() as connection:
            self.repository.get_account(account_id)
            dimensions = []
            for code in dimension_codes:
                row = connection.execute(
                    "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE", (code,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown dimension {code}")
                dimensions.append(row)
            before = self.list_account_columns(account_id)
            connection.execute(
                "DELETE FROM ledger_account_columns WHERE account_id=?", (account_id,)
            )
            connection.executemany(
                "INSERT INTO ledger_account_columns(account_id, dimension_id, position) VALUES (?, ?, ?)",
                [(account_id, item["id"], position) for position, item in enumerate(dimensions)],
            )
            result = [
                {
                    "code": item["code"],
                    "name": item["name"],
                    "active": bool(item["active"]),
                    "position": position,
                }
                for position, item in enumerate(dimensions)
            ]
            self.repository._audit(connection, "account", account_id, "columns_set", before, result)
            return result

    def set_transaction_dimension(
        self,
        transaction_id: str,
        dimension_code: str,
        value_code: str | None,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            transaction = self._versioned_transaction(connection, transaction_id, expected_version)
            dimension = connection.execute(
                "SELECT * FROM ledger_dimensions WHERE code=? COLLATE NOCASE",
                (dimension_code,),
            ).fetchone()
            if dimension is None:
                raise KeyError(f"Unknown dimension {dimension_code}")
            current = connection.execute(
                """SELECT dv.code AS value_code, td.dimension_value_id
                   FROM ledger_transaction_dimensions td
                   JOIN ledger_dimension_values dv
                     ON dv.id=td.dimension_value_id
                   WHERE td.transaction_id=? AND td.dimension_id=?""",
                (transaction_id, dimension["id"]),
            ).fetchone()
            value = None
            if value_code is not None:
                value = connection.execute(
                    """SELECT * FROM ledger_dimension_values
                       WHERE dimension_id=? AND code=? COLLATE NOCASE""",
                    (dimension["id"], value_code),
                ).fetchone()
                if value is None:
                    raise KeyError(f"Unknown value {value_code} for dimension {dimension_code}")
            if (current["value_code"] if current else None) == (value["code"] if value else None):
                return {
                    "transaction_id": transaction_id,
                    "dimension": dimension["code"],
                    "value": value["code"] if value else None,
                    "version": expected_version,
                    "changed": False,
                }
            if value is None:
                connection.execute(
                    """DELETE FROM ledger_transaction_dimensions
                       WHERE transaction_id=? AND dimension_id=?""",
                    (transaction_id, dimension["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO ledger_transaction_dimensions(
                           transaction_id, dimension_id, dimension_value_id
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(transaction_id, dimension_id) DO UPDATE SET
                           dimension_value_id=excluded.dimension_value_id,
                           created_at=CURRENT_TIMESTAMP""",
                    (transaction_id, dimension["id"], value["id"]),
                )
            version = self._touch_transaction(connection, transaction_id)
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                "dimension_clear" if value is None else "dimension_set",
                {
                    "dimension": dimension["code"],
                    "value": current["value_code"] if current else None,
                    "version": transaction["version"],
                },
                {
                    "dimension": dimension["code"],
                    "value": value["code"] if value else None,
                    "version": version,
                },
            )
            return {
                "transaction_id": transaction_id,
                "dimension": dimension["code"],
                "value": value["code"] if value else None,
                "version": version,
                "changed": True,
            }

    def create_merchant(self, canonical_name: str) -> dict[str, Any]:
        name = self._clean_name(canonical_name, "Merchant name")
        with self.repository.database.connect() as connection:
            existing = connection.execute(
                """SELECT * FROM ledger_merchants
                   WHERE canonical_name=? COLLATE NOCASE""",
                (name,),
            ).fetchone()
            if existing:
                return {**dict(existing), "created": False}
            identifier = self.repository._id("merchant")
            connection.execute(
                "INSERT INTO ledger_merchants(id, canonical_name) VALUES (?, ?)",
                (identifier, name),
            )
            self.repository._audit(
                connection, "merchant", identifier, "create", None, {"canonical_name": name}
            )
            return {"id": identifier, "canonical_name": name, "version": 1, "created": True}

    def add_merchant_alias(self, merchant_name_or_id: str, alias: str) -> dict[str, Any]:
        display_alias = self._clean_name(alias, "Merchant alias")
        normalized = normalize_merchant_alias(display_alias)
        with self.repository.database.connect() as connection:
            merchant = connection.execute(
                """SELECT * FROM ledger_merchants
                   WHERE id=? OR canonical_name=? COLLATE NOCASE""",
                (merchant_name_or_id, merchant_name_or_id),
            ).fetchone()
            if merchant is None:
                raise KeyError(f"Unknown merchant {merchant_name_or_id}")
            existing = connection.execute(
                """SELECT ma.*, m.canonical_name
                   FROM ledger_merchant_aliases ma
                   JOIN ledger_merchants m ON m.id=ma.merchant_id
                   WHERE ma.normalized_alias=?""",
                (normalized,),
            ).fetchone()
            if existing:
                if existing["merchant_id"] != merchant["id"]:
                    raise ValueError(
                        f"Alias already belongs to merchant {existing['canonical_name']}"
                    )
                return {**dict(existing), "created": False}
            identifier = self.repository._id("merchant-alias")
            connection.execute(
                """INSERT INTO ledger_merchant_aliases(
                       id, merchant_id, normalized_alias, display_alias
                   ) VALUES (?, ?, ?, ?)""",
                (identifier, merchant["id"], normalized, display_alias),
            )
            self.repository._audit(
                connection,
                "merchant_alias",
                identifier,
                "create",
                None,
                {"merchant_id": merchant["id"], "alias": display_alias},
            )
            return {
                "id": identifier,
                "merchant_id": merchant["id"],
                "canonical_name": merchant["canonical_name"],
                "alias": display_alias,
                "created": True,
            }

    def list_merchants(self) -> list[dict[str, Any]]:
        with self.repository.database.connect() as connection:
            merchants = [
                dict(row)
                for row in connection.execute(
                    """SELECT m.*, COUNT(DISTINCT t.id) AS transaction_count
                       FROM ledger_merchants m
                       LEFT JOIN ledger_transactions t ON t.merchant_id=m.id
                       GROUP BY m.id ORDER BY m.canonical_name COLLATE NOCASE"""
                )
            ]
            for merchant in merchants:
                merchant["aliases"] = [
                    row["display_alias"]
                    for row in connection.execute(
                        """SELECT display_alias FROM ledger_merchant_aliases
                           WHERE merchant_id=? ORDER BY display_alias COLLATE NOCASE""",
                        (merchant["id"],),
                    )
                ]
            return merchants

    def set_transaction_merchant(
        self,
        transaction_id: str,
        merchant_name_or_id: str,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            transaction = self._versioned_transaction(connection, transaction_id, expected_version)
            merchant = connection.execute(
                """SELECT * FROM ledger_merchants
                   WHERE id=? OR canonical_name=? COLLATE NOCASE""",
                (merchant_name_or_id, merchant_name_or_id),
            ).fetchone()
            if merchant is None:
                raise KeyError(f"Unknown merchant {merchant_name_or_id}")
            if transaction["merchant_id"] == merchant["id"]:
                return {
                    "transaction_id": transaction_id,
                    "merchant": merchant["canonical_name"],
                    "version": expected_version,
                    "changed": False,
                }
            connection.execute(
                "UPDATE ledger_transactions SET merchant_id=? WHERE id=?",
                (merchant["id"], transaction_id),
            )
            version = self._touch_transaction(connection, transaction_id)
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                "merchant_set",
                {"merchant_id": transaction["merchant_id"], "version": transaction["version"]},
                {
                    "merchant_id": merchant["id"],
                    "merchant": merchant["canonical_name"],
                    "version": version,
                },
            )
            return {
                "transaction_id": transaction_id,
                "merchant": merchant["canonical_name"],
                "version": version,
                "changed": True,
            }

    def set_transaction_note(
        self, transaction_id: str, note: str, *, expected_version: int
    ) -> dict[str, Any]:
        if len(note) > 2000:
            raise ValueError("Transaction note must not exceed 2000 characters")
        with self.repository.database.connect() as connection:
            transaction = self._versioned_transaction(connection, transaction_id, expected_version)
            if transaction["note"] == note:
                return {
                    "transaction_id": transaction_id,
                    "note": note,
                    "version": expected_version,
                    "changed": False,
                }
            connection.execute(
                "UPDATE ledger_transactions SET note=? WHERE id=?", (note, transaction_id)
            )
            version = self._touch_transaction(connection, transaction_id)
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                "note_set",
                {"note": transaction["note"], "version": transaction["version"]},
                {"note": note, "version": version},
            )
            return {
                "transaction_id": transaction_id,
                "note": note,
                "version": version,
                "changed": True,
            }

    def update_manual_transaction(
        self,
        transaction_id: str,
        *,
        expected_version: int,
        booking_date: date | None = None,
        amount: Decimal | None = None,
        purpose: str | None = None,
        counterparty: str | None = None,
        reference: str | None = None,
        valuta_date: date | None = None,
    ) -> dict[str, Any]:
        with self.repository.database.connect() as connection:
            transaction = self._versioned_transaction(connection, transaction_id, expected_version)
            account = connection.execute(
                """SELECT p.kind AS provider_kind FROM ledger_accounts a
                   JOIN ledger_providers p ON p.id=a.provider_id WHERE a.id=?""",
                (transaction["account_id"],),
            ).fetchone()
            if account is None:
                raise KeyError(f"Unknown account {transaction['account_id']}")
            if account["provider_kind"] != "manual":
                raise ValueError(
                    "Manual transactions are only allowed on manual accounts; use import preview or provider sync for bank accounts"
                )
            if transaction["state"] != "active":
                raise ValueError("Manual transaction must be active to update it")
            amount_minor = (
                to_minor_units(amount) if amount is not None else transaction["amount_minor"]
            )
            if amount_minor == 0:
                raise ValueError("Manual transactions must have a non-zero amount")
            updated = {
                "booking_date": booking_date.isoformat()
                if booking_date
                else transaction["booking_date"],
                "valuta_date": valuta_date.isoformat()
                if valuta_date
                else transaction["valuta_date"],
                "amount_minor": amount_minor,
                "purpose": self._clean_name(purpose, "Purpose")
                if purpose is not None
                else transaction["purpose"],
                "counterparty": counterparty
                if counterparty is not None
                else transaction["counterparty"],
                "reference": reference if reference is not None else transaction["reference"],
            }
            before = {key: transaction[key] for key in updated}
            if before == updated:
                return {
                    "transaction_id": transaction_id,
                    "version": expected_version,
                    "changed": False,
                }
            connection.execute(
                """UPDATE ledger_transactions SET booking_date=?, valuta_date=?, amount_minor=?, purpose=?, counterparty=?, reference=? WHERE id=?""",
                (*updated.values(), transaction_id),
            )
            version = self._touch_transaction(connection, transaction_id)
            payload = {
                **updated,
                "currency": transaction["currency"],
                "transaction_id": transaction_id,
            }
            connection.execute(
                """INSERT INTO ledger_source_transaction_revisions(
                       id, source_transaction_id, idempotency_key, raw_payload_json,
                       normalized_payload_json, booking_status
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self.repository._id("source-revision"),
                    transaction["source_transaction_id"],
                    f"manual-revision:{transaction_id}:{version}",
                    json.dumps(payload, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    transaction["booking_status"],
                ),
            )
            self.repository._audit(
                connection,
                "transaction",
                transaction_id,
                "manual_transaction_update",
                before,
                {**updated, "version": version},
                actor="manual",
            )
            return {"transaction_id": transaction_id, "version": version, "changed": True}

    def list_account_field_options(self, account_id: str) -> dict[str, tuple[str, ...]]:
        self.repository.get_account(account_id)
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                """SELECT field_kind, field_code FROM ledger_account_field_options
                   WHERE account_id=? ORDER BY field_kind, field_code""",
                (account_id,),
            )
            result: dict[str, list[str]] = {"category": [], "tag": [], "dimension_value": []}
            for row in rows:
                result[row["field_kind"]].append(row["field_code"])
        return {kind: tuple(codes) for kind, codes in result.items()}

    def set_account_field_options(
        self, account_id: str, field_kind: str, codes: Iterable[str]
    ) -> dict[str, Any]:
        if field_kind not in {"category", "tag", "dimension_value"}:
            raise ValueError("Unknown account field option kind")
        requested = tuple(dict.fromkeys(codes))
        self.repository.get_account(account_id)
        with self.repository.database.connect() as connection:
            for code in requested:
                if not self._account_field_option_exists(connection, field_kind, code):
                    raise KeyError(f"Unknown {field_kind} option {code}")
            before = [
                row["field_code"]
                for row in connection.execute(
                    "SELECT field_code FROM ledger_account_field_options WHERE account_id=? AND field_kind=?",
                    (account_id, field_kind),
                )
            ]
            connection.execute(
                "DELETE FROM ledger_account_field_options WHERE account_id=? AND field_kind=?",
                (account_id, field_kind),
            )
            connection.executemany(
                "INSERT INTO ledger_account_field_options(account_id, field_kind, field_code) VALUES (?, ?, ?)",
                [(account_id, field_kind, code) for code in requested],
            )
            self.repository._audit(
                connection,
                "account",
                account_id,
                "account_field_options_set",
                {"field_kind": field_kind, "codes": before},
                {"field_kind": field_kind, "codes": requested},
            )
        return {"account_id": account_id, "field_kind": field_kind, "codes": requested}

    @staticmethod
    def _account_field_option_exists(
        connection: sqlite3.Connection, field_kind: str, code: str
    ) -> bool:
        if field_kind == "category":
            profile, separator, category = code.partition(":")
            return bool(
                separator
                and connection.execute(
                    """SELECT 1 FROM ledger_categories c
                       JOIN ledger_profile_categories pc ON pc.category_id=c.id
                       JOIN ledger_profiles p ON p.id=pc.profile_id
                       WHERE p.code=? AND c.code=?""",
                    (profile, category),
                ).fetchone()
            )
        if field_kind == "tag":
            return bool(
                connection.execute(
                    "SELECT 1 FROM ledger_tags WHERE name=? COLLATE NOCASE", (code,)
                ).fetchone()
            )
        dimension, separator, value = code.partition(":")
        return bool(
            separator
            and connection.execute(
                """SELECT 1 FROM ledger_dimension_values dv JOIN ledger_dimensions d ON d.id=dv.dimension_id WHERE d.code=? AND dv.code=?""",
                (dimension, value),
            ).fetchone()
        )

    def list_profile_categories(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        with self.repository.database.connect() as connection:
            for row in connection.execute(
                """SELECT p.code AS profile_code, p.name AS profile_name,
                          c.code AS category_code, c.name AS category_name,
                          parent.code AS parent_code
                   FROM ledger_categories c
                   JOIN ledger_profile_categories pc ON pc.category_id=c.id
                   JOIN ledger_profiles p ON p.id=pc.profile_id
                   LEFT JOIN ledger_categories parent ON parent.id=c.parent_id
                   ORDER BY p.code, c.code"""
            ):
                grouped.setdefault(row["profile_code"], []).append(
                    {
                        "code": row["category_code"],
                        "name": row["category_name"],
                        "parent_code": row["parent_code"],
                    }
                )
        for entries in grouped.values():
            by_code = {entry["code"]: entry for entry in entries}
            for entry in entries:
                path: list[str] = []
                current: dict[str, Any] | None = entry
                visited: set[str] = set()
                while current is not None and current["code"] not in visited:
                    visited.add(current["code"])
                    path.append(current["name"])
                    current = by_code.get(current["parent_code"])
                entry["path"] = list(reversed(path))
                entry["depth"] = len(path) - 1
            entries.sort(key=lambda entry: [part.casefold() for part in entry["path"]])
        return grouped

    def account_field_options_view(self, account_id: str) -> dict[str, Any]:
        """Values a GUI may offer for this account: whitelist if set, otherwise everything."""
        options = self.list_account_field_options(account_id)
        allowed_categories = {code for code in options["category"]}
        allowed_dimension_values = {code for code in options["dimension_value"]}
        categories = {
            profile: [
                category
                for category in entries
                if not _restricts(allowed_categories, profile)
                or f"{profile}:{category['code']}" in allowed_categories
            ]
            for profile, entries in self.list_profile_categories().items()
        }
        columns = self.list_account_columns(account_id)
        configured_codes = {column["code"] for column in columns}
        dimensions = self.list_dimensions()
        if configured_codes:
            positions = {column["code"]: column["position"] for column in columns}
            dimensions = sorted(
                (item for item in dimensions if item["code"] in configured_codes),
                key=lambda item: positions[item["code"]],
            )
        dimension_values = {
            dimension["code"]: [
                {"code": value["code"], "name": value["name"]}
                for value in dimension["values"]
                if dimension["active"] and value["active"]
                if not _restricts(allowed_dimension_values, dimension["code"])
                or f"{dimension['code']}:{value['code']}" in allowed_dimension_values
            ]
            for dimension in dimensions
        }
        tags = [row["name"] for row in self.list_tags()]
        return {
            "category": {profile: entries for profile, entries in categories.items() if entries},
            "tag": list(options["tag"]) or tags,
            "dimension_value": {
                code: values for code, values in dimension_values.items() if values
            },
            "dimensions": [
                {"code": item["code"], "position": position}
                for position, item in enumerate(dimensions)
                if dimension_values.get(item["code"])
            ],
        }

    def search_transactions(
        self,
        filters: TransactionSearchFilter,
        *,
        account_scope: AccountScope | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "date",
        sort_direction: str = "desc",
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must not be negative")
        account_ids = self.repository.resolve_account_scope(account_scope or AccountScope.all())
        if not account_ids:
            return []
        sort_columns = {
            "date": "t.booking_date",
            "amount": "t.amount_minor",
            "account": "a.display_name COLLATE NOCASE",
            "counterparty": "COALESCE(t.counterparty, t.purpose) COLLATE NOCASE",
            "category": "COALESCE(c.name, '') COLLATE NOCASE",
        }
        if sort_by not in sort_columns:
            raise ValueError("Unsupported transaction sort column")
        direction = sort_direction.lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        sql_direction = direction.upper()
        source, parameters = build_transaction_query(filters, account_ids)
        query = f"""{SEARCH_COLUMNS} {source}
                    ORDER BY {sort_columns[sort_by]} {sql_direction},
                             t.id {sql_direction} LIMIT ? OFFSET ?"""
        parameters.extend((limit, offset))
        with self.repository.database.connect() as connection:
            return [self._decode_record(dict(row)) for row in connection.execute(query, parameters)]

    def count_transactions(
        self, filters: TransactionSearchFilter, *, account_scope: AccountScope | None = None
    ) -> int:
        account_ids = self.repository.resolve_account_scope(account_scope or AccountScope.all())
        query, parameters = build_transaction_query(filters, account_ids)
        with self.repository.database.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) {query}", parameters).fetchone()[0])

    def transaction_search_record(self, transaction_id: str) -> dict[str, Any]:
        transaction = self.repository.get_transaction(transaction_id)
        source, parameters = build_transaction_query(
            TransactionSearchFilter(), (transaction["account_id"],)
        )
        parameters.append(transaction_id)
        with self.repository.database.connect() as connection:
            row = connection.execute(f"{SEARCH_COLUMNS} {source} AND t.id=?", parameters).fetchone()
        if row is None:
            raise KeyError(f"Unknown active transaction {transaction_id}")
        return self._decode_record(dict(row))

    def list_review_items(
        self,
        *,
        status: str = "open",
        item_type: str | None = None,
        account_scope: AccountScope | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status not in {"open", "resolved", "dismissed"}:
            raise ValueError("Unknown review status")
        if item_type and item_type not in REVIEW_ITEM_TYPES:
            raise ValueError("Unknown review item type")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.repository.database.connect() as connection:
            self._refresh_review_items(connection)
            query = "SELECT ri.* FROM ledger_review_items ri WHERE ri.status=?"
            parameters: list[Any] = [status]
            if item_type:
                query += " AND ri.item_type=?"
                parameters.append(item_type)
            account_ids = self.repository.resolve_account_scope(account_scope or AccountScope.all())
            if not account_ids:
                query += """ AND ri.entity_type IN (
                    'document', 'import_preview', 'system'
                )"""
            else:
                placeholders = ",".join("?" for _ in account_ids)
                query += f""" AND (
                (ri.entity_type='transaction' AND EXISTS (
                    SELECT 1 FROM ledger_transactions t
                    WHERE t.id=ri.entity_id
                      AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='classification' AND EXISTS (
                    SELECT 1 FROM ledger_classifications cl
                    JOIN ledger_transactions t ON t.id=cl.transaction_id
                    WHERE cl.id=ri.entity_id
                      AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='transaction_relation' AND EXISTS (
                    SELECT 1 FROM ledger_transaction_relations tr
                    JOIN ledger_transactions s ON s.id=tr.source_transaction_id
                    JOIN ledger_transactions target ON target.id=tr.target_transaction_id
                    WHERE tr.id=ri.entity_id
                      AND (s.account_id IN ({placeholders})
                           OR target.account_id IN ({placeholders}))
                )) OR
                (ri.entity_type='document_match' AND EXISTS (
                    SELECT 1 FROM ledger_document_matches dm
                    JOIN ledger_transactions t ON t.id=dm.transaction_id
                    WHERE dm.id=ri.entity_id
                      AND t.account_id IN ({placeholders})
                )) OR
                (ri.entity_type='reconciliation' AND EXISTS (
                    SELECT 1 FROM ledger_reconciliations rec
                    WHERE rec.id=ri.entity_id
                      AND rec.account_id IN ({placeholders})
                )) OR
                ri.entity_type IN ('document', 'import_preview', 'system')
                )"""
                parameters.extend(account_ids * 6)
            query += " ORDER BY ri.priority DESC, ri.created_at DESC, ri.id LIMIT ?"
            parameters.append(limit)
            return [self._decode_record(dict(row)) for row in connection.execute(query, parameters)]

    def review_item(
        self, review_id: str, decision: str, *, expected_version: int
    ) -> dict[str, Any]:
        if decision not in {"resolve", "dismiss"}:
            raise ValueError("Decision must be resolve or dismiss")
        with self.repository.database.connect() as connection:
            item = connection.execute(
                "SELECT * FROM ledger_review_items WHERE id=?", (review_id,)
            ).fetchone()
            if item is None:
                raise KeyError(f"Unknown review item {review_id}")
            if item["version"] != expected_version:
                raise VersionConflictError("The review item has changed; reload it before writing.")
            if item["status"] != "open":
                raise ValueError("Only open review items can be changed")
            status = "resolved" if decision == "resolve" else "dismissed"
            connection.execute(
                """UPDATE ledger_review_items
                   SET status=?, version=version+1,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, review_id),
            )
            self.repository._audit(
                connection,
                "review_item",
                review_id,
                decision,
                {"status": "open", "version": expected_version},
                {"status": status, "version": expected_version + 1},
            )
            return {"review_id": review_id, "status": status, "version": expected_version + 1}

    @staticmethod
    def _enqueue_review(
        connection: sqlite3.Connection,
        item_type: str,
        entity_type: str,
        entity_id: str,
        reason: str,
        priority: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        identifier = (
            "review-"
            + hashlib.sha256(f"{item_type}|{entity_type}|{entity_id}".encode()).hexdigest()[:32]
        )
        connection.execute(
            """INSERT OR IGNORE INTO ledger_review_items(
                   id, item_type, entity_type, entity_id, reason, priority,
                   metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                identifier,
                item_type,
                entity_type,
                entity_id,
                reason,
                priority,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    def _refresh_review_items(self, connection: sqlite3.Connection) -> None:
        sources = (
            (
                "pending_transaction",
                "transaction",
                "ledger_transactions",
                "id",
                "booking_status='pending' AND state<>'void'",
                "Pending transaction requires final review",
                80,
            ),
            (
                "classification_suggestion",
                "classification",
                "ledger_classifications",
                "id",
                "status='suggested'",
                "Review classification suggestion",
                70,
            ),
            (
                "relation_suggestion",
                "transaction_relation",
                "ledger_transaction_relations",
                "id",
                "status='suggested'",
                "Review transaction relation",
                70,
            ),
            (
                "document_match",
                "document_match",
                "ledger_document_matches",
                "id",
                "status='suggested'",
                "Review document match",
                65,
            ),
            (
                "unmatched_document",
                "document",
                "ledger_documents",
                "id",
                "status='open'",
                "Match open document",
                60,
            ),
            (
                "reconciliation_difference",
                "reconciliation",
                "ledger_reconciliations",
                "id",
                "status='difference'",
                "Review balance difference",
                90,
            ),
        )
        for item_type, entity_type, table, id_column, condition, reason, priority in sources:
            rows = connection.execute(
                f"SELECT {id_column} AS id FROM {table} WHERE {condition}"
            ).fetchall()
            for row in rows:
                self._enqueue_review(
                    connection, item_type, entity_type, row["id"], reason, priority
                )
        resolved_checks = (
            (
                "pending_transaction",
                "transaction",
                "ledger_transactions",
                "booking_status<>'pending' OR state='void'",
            ),
            (
                "classification_suggestion",
                "classification",
                "ledger_classifications",
                "status<>'suggested'",
            ),
            (
                "relation_suggestion",
                "transaction_relation",
                "ledger_transaction_relations",
                "status<>'suggested'",
            ),
            ("document_match", "document_match", "ledger_document_matches", "status<>'suggested'"),
            ("unmatched_document", "document", "ledger_documents", "status<>'open'"),
            (
                "reconciliation_difference",
                "reconciliation",
                "ledger_reconciliations",
                "status<>'difference'",
            ),
        )
        for item_type, entity_type, table, condition in resolved_checks:
            connection.execute(
                f"""UPDATE ledger_review_items
                    SET status='resolved', version=version+1,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE item_type=? AND entity_type=? AND status='open'
                      AND EXISTS (SELECT 1 FROM {table} source
                                  WHERE source.id=ledger_review_items.entity_id
                                    AND ({condition}))""",
                (item_type, entity_type),
            )

    def create_bulk_preview(
        self,
        action: str | BulkAction,
        transaction_ids: Iterable[str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            normalized_action = BulkAction(action).value
        except ValueError as error:
            raise ValueError("Unknown bulk action") from error
        identifiers = tuple(dict.fromkeys(transaction_ids))
        if not identifiers:
            raise ValueError("Bulk preview requires at least one transaction")
        if len(identifiers) > 500:
            raise ValueError("Bulk preview is limited to 500 transactions")
        with self.repository.database.connect() as connection:
            ordered = self._bulk_selection_rows(connection, identifiers)
            if normalized_action == BulkAction.EDIT_FIELDS.value:
                normalized_payload = self._validate_edit_fields_payload(
                    connection, payload, ordered
                )
                changes = self._edit_fields_preview(ordered, normalized_payload)
            else:
                normalized_payload = self._validate_bulk_payload(
                    connection, normalized_action, payload
                )
                changes = None
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "action": normalized_action,
                        "payload": normalized_payload,
                        "versions": [(row["id"], row["version"]) for row in ordered],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            preview_id = self.repository._id("bulk-preview")
            connection.execute(
                """INSERT INTO ledger_bulk_previews(
                       id, action, payload_json, transaction_count,
                       version_fingerprint
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    preview_id,
                    normalized_action,
                    json.dumps(normalized_payload, sort_keys=True),
                    len(ordered),
                    fingerprint,
                ),
            )
            connection.executemany(
                """INSERT INTO ledger_bulk_preview_items(
                       preview_id, transaction_id, expected_version
                   ) VALUES (?, ?, ?)""",
                [(preview_id, row["id"], row["version"]) for row in ordered],
            )
            self.repository._audit(
                connection,
                "bulk_preview",
                preview_id,
                "create",
                None,
                {
                    "action": normalized_action,
                    "payload": normalized_payload,
                    "transaction_count": len(ordered),
                    "fingerprint": fingerprint,
                },
            )
            result = {
                "preview_id": preview_id,
                "action": normalized_action,
                "payload": normalized_payload,
                "transaction_count": len(ordered),
                "version_fingerprint": fingerprint,
                "transactions": [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "version",
                            "booking_date",
                            "amount_minor",
                            "currency",
                            "purpose",
                            "account_id",
                        )
                    }
                    for row in ordered
                ],
            }
            if changes is not None:
                result.update(changes)
            return result

    def bulk_edit_context(self, transaction_ids: Iterable[str]) -> dict[str, Any]:
        identifiers = tuple(dict.fromkeys(transaction_ids))
        if not identifiers:
            raise ValueError("Bulk context requires at least one transaction")
        if len(identifiers) > 500:
            raise ValueError("Bulk context is limited to 500 transactions")
        with self.repository.database.connect() as connection:
            rows = self._bulk_selection_rows(connection, identifiers)
            return self._bulk_edit_context_from_rows(connection, rows)

    @staticmethod
    def _common_field(values: list[Any]) -> dict[str, Any]:
        if all(value in (None, "") for value in values):
            return {"state": "empty", "value": None}
        if all(value == values[0] for value in values[1:]):
            return {"state": "common", "value": values[0]}
        return {"state": "mixed", "value": None}

    def _bulk_selection_rows(
        self, connection: sqlite3.Connection, identifiers: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"""SELECT t.id, t.version, t.booking_date, t.amount_minor,
                       t.currency, t.purpose, t.account_id, t.state,
                       t.booking_status, t.note, t.merchant_id,
                       a.display_name AS account_name,
                       m.canonical_name AS merchant,
                       cl.profile_id, cp.code AS classification_profile,
                       cl.category_id, c.code AS category_code,
                       COALESCE((SELECT json_group_array(name) FROM (
                           SELECT tag.name AS name
                           FROM ledger_transaction_tags tt
                           JOIN ledger_tags tag ON tag.id=tt.tag_id
                           WHERE tt.transaction_id=t.id
                           ORDER BY tag.name COLLATE NOCASE
                       )), '[]') AS tags_json,
                       COALESCE((SELECT json_group_object(code, value_code) FROM (
                           SELECT d.code AS code, dv.code AS value_code
                           FROM ledger_transaction_dimensions td
                           JOIN ledger_dimensions d ON d.id=td.dimension_id
                           JOIN ledger_dimension_values dv
                             ON dv.id=td.dimension_value_id
                           WHERE td.transaction_id=t.id ORDER BY d.code
                       )), '{{}}') AS dimensions_json
                FROM ledger_transactions t
                JOIN ledger_accounts a ON a.id=t.account_id
                LEFT JOIN ledger_merchants m ON m.id=t.merchant_id
                LEFT JOIN ledger_classifications cl ON cl.id=(
                    SELECT latest.id FROM ledger_classifications latest
                    WHERE latest.transaction_id=t.id AND latest.status='applied'
                    ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
                )
                LEFT JOIN ledger_profiles cp ON cp.id=cl.profile_id
                LEFT JOIN ledger_categories c ON c.id=cl.category_id
                WHERE t.id IN ({placeholders})""",
            identifiers,
        ).fetchall()
        found = {row["id"]: self._decode_record(dict(row)) for row in rows}
        missing = [identifier for identifier in identifiers if identifier not in found]
        if missing:
            raise KeyError("Unknown transactions: " + ", ".join(missing))
        invalid = [identifier for identifier in identifiers if found[identifier]["state"] == "void"]
        if invalid:
            raise ValueError("Void transactions cannot be changed: " + ", ".join(invalid))
        return [found[identifier] for identifier in identifiers]

    def _bulk_edit_context_from_rows(
        self, connection: sqlite3.Connection, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        account_ids = tuple(dict.fromkeys(row["account_id"] for row in rows))
        placeholders = ",".join("?" for _ in account_ids)
        allowed: dict[str, dict[str, set[str]]] = {
            account_id: {"category": set(), "tag": set(), "dimension_value": set()}
            for account_id in account_ids
        }
        for row in connection.execute(
            f"""SELECT account_id, field_kind, field_code
                FROM ledger_account_field_options
                WHERE account_id IN ({placeholders})""",
            account_ids,
        ):
            allowed[row["account_id"]][row["field_kind"]].add(row["field_code"])

        categories: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute(
            """SELECT p.code AS profile, c.code, c.name
               FROM ledger_profile_categories pc
               JOIN ledger_profiles p ON p.id=pc.profile_id
               JOIN ledger_categories c ON c.id=pc.category_id
               ORDER BY p.code, c.name COLLATE NOCASE"""
        ):
            option = f"{row['profile']}:{row['code']}"
            if all(
                not _restricts(allowed[account_id]["category"], row["profile"])
                or option in allowed[account_id]["category"]
                for account_id in account_ids
            ):
                categories.setdefault(row["profile"], []).append(
                    {"code": row["code"], "name": row["name"]}
                )

        all_tags = [
            dict(row)
            for row in connection.execute(
                "SELECT id, name FROM ledger_tags ORDER BY name COLLATE NOCASE"
            )
        ]
        tags = [
            tag["name"]
            for tag in all_tags
            if all(
                not allowed[account_id]["tag"] or tag["name"] in allowed[account_id]["tag"]
                for account_id in account_ids
            )
        ]

        configured: dict[str, set[str]] = {account_id: set() for account_id in account_ids}
        for row in connection.execute(
            f"""SELECT ac.account_id, d.code
                FROM ledger_account_columns ac
                JOIN ledger_dimensions d ON d.id=ac.dimension_id
                WHERE ac.account_id IN ({placeholders})""",
            account_ids,
        ):
            configured[row["account_id"]].add(row["code"])
        dimensions: dict[str, list[dict[str, str]]] = {}
        for row in connection.execute(
            """SELECT d.code AS dimension, dv.code, dv.name
               FROM ledger_dimensions d
               JOIN ledger_dimension_values dv ON dv.dimension_id=d.id
               WHERE d.active=1 AND dv.active=1
               ORDER BY d.code, dv.name COLLATE NOCASE"""
        ):
            option = f"{row['dimension']}:{row['code']}"
            if all(
                (not configured[account_id] or row["dimension"] in configured[account_id])
                and (
                    not _restricts(allowed[account_id]["dimension_value"], row["dimension"])
                    or option in allowed[account_id]["dimension_value"]
                )
                for account_id in account_ids
            ):
                dimensions.setdefault(row["dimension"], []).append(
                    {"code": row["code"], "name": row["name"]}
                )

        status_sets = []
        for row in rows:
            targets = (
                set()
                if row["state"] == "split_parent"
                else {
                    target.value
                    for target in VALID_TRANSACTION_STATUS_TRANSITIONS[
                        transaction_status(row["booking_status"])
                    ]
                }
            )
            status_sets.append(targets)
        statuses = sorted(set.intersection(*status_sets)) if status_sets else []

        tag_counts: dict[str, int] = {}
        for row in rows:
            for tag in row["tags"]:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        category_values = [
            (
                {
                    "profile": row["classification_profile"],
                    "category": row["category_code"],
                }
                if row["category_id"]
                else None
            )
            for row in rows
        ]
        dimension_codes = sorted(set(dimensions).union(*(set(row["dimensions"]) for row in rows)))
        totals: dict[str, int] = {}
        for row in rows:
            totals[row["currency"]] = totals.get(row["currency"], 0) + row["amount_minor"]
        account_names = {row["account_id"]: row["account_name"] for row in rows}
        return {
            "transaction_count": len(rows),
            "account_count": len(account_ids),
            "accounts": [
                {"id": account_id, "name": account_names[account_id]} for account_id in account_ids
            ],
            "booking_from": min(row["booking_date"] for row in rows),
            "booking_to": max(row["booking_date"] for row in rows),
            "amount_totals": [
                {"currency": currency, "amount_minor": amount}
                for currency, amount in sorted(totals.items())
            ],
            "current": {
                "category": self._common_field(category_values),
                "merchant": self._common_field([row["merchant"] for row in rows]),
                "note": self._common_field([row["note"] for row in rows]),
                "status": self._common_field([row["booking_status"] for row in rows]),
                "tags": {
                    "common": sorted(
                        tag for tag, count in tag_counts.items() if count == len(rows)
                    ),
                    "partial": sorted(
                        tag for tag, count in tag_counts.items() if count != len(rows)
                    ),
                },
                "dimensions": {
                    code: self._common_field([row["dimensions"].get(code) for row in rows])
                    for code in dimension_codes
                },
            },
            "options": {
                "category": categories,
                "tags": tags,
                "merchants": [
                    row["canonical_name"]
                    for row in connection.execute(
                        """SELECT canonical_name FROM ledger_merchants
                           ORDER BY canonical_name COLLATE NOCASE"""
                    )
                ],
                "dimensions": dimensions,
                "statuses": statuses,
            },
            "disabled": {
                "category": (
                    None if categories else "No category is allowed for all selected accounts."
                ),
                "tags": (None if tags else "No tag is allowed for all selected accounts."),
                "dimensions": (
                    None if dimensions else "No dimension is available for all selected accounts."
                ),
                "status": (
                    None
                    if statuses
                    else "No status transition is allowed for the entire selection."
                ),
            },
        }

    def _validate_edit_fields_payload(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        allowed_fields = {"category", "tags", "merchant", "dimensions", "note", "status"}
        unknown = sorted(set(payload) - allowed_fields)
        if unknown:
            raise ValueError("Unknown edit fields: " + ", ".join(unknown))
        context = self._bulk_edit_context_from_rows(connection, rows)
        options = context["options"]
        normalized: dict[str, Any] = {}

        if "category" in payload:
            category = payload["category"]
            if not isinstance(category, dict):
                raise ValueError("category must be an object")
            profile = str(category.get("profile") or "").strip()
            code = str(category.get("category") or "").strip()
            if not any(item["code"] == code for item in options["category"].get(profile, [])):
                raise ValueError("Category is not allowed for every selected account")
            record = connection.execute(
                """SELECT p.id AS profile_id, c.id AS category_id
                   FROM ledger_profiles p
                   JOIN ledger_profile_categories pc ON pc.profile_id=p.id
                   JOIN ledger_categories c ON c.id=pc.category_id
                   WHERE p.code=? AND c.code=?""",
                (profile, code),
            ).fetchone()
            if record is None:
                raise ValueError("Category does not belong to the selected profile")
            normalized["category"] = {
                "profile": profile,
                "category": code,
                "profile_id": record["profile_id"],
                "category_id": record["category_id"],
            }

        if "tags" in payload:
            tags = payload["tags"]
            if not isinstance(tags, dict):
                raise ValueError("tags must be an object")
            extra = sorted(set(tags) - {"add", "remove"})
            if extra:
                raise ValueError("Unknown tag operations: " + ", ".join(extra))
            requested: dict[str, list[dict[str, str]]] = {"add": [], "remove": []}
            option_names = {name.casefold(): name for name in options["tags"]}
            for operation in ("add", "remove"):
                values = tags.get(operation, [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) for value in values
                ):
                    raise ValueError(f"tags.{operation} must be a string array")
                for raw in dict.fromkeys(value.strip() for value in values if value.strip()):
                    canonical = option_names.get(raw.casefold())
                    if canonical is None:
                        raise ValueError(f"Tag {raw} is not allowed for every selected account")
                    record = connection.execute(
                        "SELECT id, name FROM ledger_tags WHERE name=? COLLATE NOCASE",
                        (canonical,),
                    ).fetchone()
                    if record is None:
                        raise KeyError(f"Unknown tag {raw}")
                    requested[operation].append({"id": record["id"], "name": record["name"]})
            overlap = {item["id"] for item in requested["add"]} & {
                item["id"] for item in requested["remove"]
            }
            if overlap:
                raise ValueError("A tag cannot be added and removed in the same patch")
            if requested["add"] or requested["remove"]:
                normalized["tags"] = requested

        if "merchant" in payload:
            merchant = payload["merchant"]
            if not isinstance(merchant, dict):
                raise ValueError("merchant must be an object")
            mode = str(merchant.get("mode") or "")
            if mode not in {"set", "clear"}:
                raise ValueError("merchant.mode must be set or clear")
            normalized_merchant: dict[str, Any] = {"mode": mode}
            if mode == "set":
                value = str(merchant.get("value") or "").strip()
                record = connection.execute(
                    """SELECT id, canonical_name FROM ledger_merchants
                       WHERE id=? OR canonical_name=? COLLATE NOCASE""",
                    (value, value),
                ).fetchone()
                if record is None:
                    raise KeyError(f"Unknown merchant {value}")
                normalized_merchant.update({"id": record["id"], "value": record["canonical_name"]})
            normalized["merchant"] = normalized_merchant

        if "dimensions" in payload:
            dimensions = payload["dimensions"]
            if not isinstance(dimensions, dict):
                raise ValueError("dimensions must be an object")
            normalized_dimensions: dict[str, Any] = {}
            for dimension_code, operation in dimensions.items():
                if not isinstance(operation, dict):
                    raise ValueError("Every dimension operation must be an object")
                if dimension_code not in options["dimensions"]:
                    raise ValueError(
                        f"Dimension {dimension_code} is not available for every selected account"
                    )
                mode = str(operation.get("mode") or "")
                if mode not in {"set", "clear"}:
                    raise ValueError("dimension mode must be set or clear")
                dimension = connection.execute(
                    "SELECT id FROM ledger_dimensions WHERE code=? COLLATE NOCASE",
                    (dimension_code,),
                ).fetchone()
                if dimension is None:
                    raise KeyError(f"Unknown dimension {dimension_code}")
                normalized_operation: dict[str, Any] = {
                    "mode": mode,
                    "dimension_id": dimension["id"],
                }
                if mode == "set":
                    value = str(operation.get("value") or "").strip()
                    if not any(
                        item["code"] == value for item in options["dimensions"][dimension_code]
                    ):
                        raise ValueError(f"Dimension value {dimension_code}/{value} is not allowed")
                    value_record = connection.execute(
                        """SELECT dv.id, dv.code FROM ledger_dimension_values dv
                           JOIN ledger_dimensions d ON d.id=dv.dimension_id
                           WHERE d.code=? COLLATE NOCASE AND dv.code=? COLLATE NOCASE""",
                        (dimension_code, value),
                    ).fetchone()
                    if value_record is None:
                        raise KeyError(f"Unknown dimension value {dimension_code}/{value}")
                    normalized_operation.update(
                        {"value": value_record["code"], "value_id": value_record["id"]}
                    )
                normalized_dimensions[dimension_code] = normalized_operation
            if normalized_dimensions:
                normalized["dimensions"] = normalized_dimensions

        if "note" in payload:
            note = payload["note"]
            if not isinstance(note, dict):
                raise ValueError("note must be an object")
            mode = str(note.get("mode") or "")
            if mode not in {"replace", "append", "clear"}:
                raise ValueError("note.mode must be replace, append or clear")
            text = str(note.get("text") or "") if mode != "clear" else ""
            if mode in {"replace", "append"} and not text:
                raise ValueError("note.text must not be empty")
            if any(len(self._patched_note(row["note"], mode, text)) > 2000 for row in rows):
                raise ValueError("Resulting transaction notes must not exceed 2000 characters")
            normalized["note"] = {"mode": mode, "text": text}

        if "status" in payload:
            status = payload["status"]
            if not isinstance(status, dict):
                raise ValueError("status must be an object")
            value = str(status.get("value") or "")
            if value not in options["statuses"]:
                raise ValueError("Status transition is not valid for every selected transaction")
            normalized["status"] = {"value": value}

        if not normalized:
            raise ValueError("Edit patch must change at least one field")
        return normalized

    @staticmethod
    def _patched_note(current: str, mode: str, text: str) -> str:
        if mode == "clear":
            return ""
        if mode == "replace":
            return text
        return f"{current}\n{text}" if current else text

    @staticmethod
    def _raw_edit_fields_payload(payload: dict[str, Any]) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if category := payload.get("category"):
            raw["category"] = {
                "profile": category["profile"],
                "category": category["category"],
            }
        if tags := payload.get("tags"):
            raw["tags"] = {
                operation: [item["name"] for item in tags[operation]]
                for operation in ("add", "remove")
            }
        if merchant := payload.get("merchant"):
            raw["merchant"] = {"mode": merchant["mode"]}
            if merchant["mode"] == "set":
                raw["merchant"]["value"] = merchant["value"]
        if dimensions := payload.get("dimensions"):
            raw["dimensions"] = {
                code: {
                    "mode": operation["mode"],
                    **({"value": operation["value"]} if operation["mode"] == "set" else {}),
                }
                for code, operation in dimensions.items()
            }
        if note := payload.get("note"):
            raw["note"] = dict(note)
        if status := payload.get("status"):
            raw["status"] = dict(status)
        return raw

    def _edit_fields_preview(
        self, rows: list[dict[str, Any]], payload: dict[str, Any]
    ) -> dict[str, Any]:
        affected: dict[str, set[str]] = {
            field: set()
            for field in ("category", "tags", "merchant", "note", "status")
            if field in payload
        }
        affected.update({f"dimension:{code}": set() for code in payload.get("dimensions", {})})

        def mark(field: str, row: dict[str, Any], changed: bool) -> None:
            if changed:
                affected.setdefault(field, set()).add(row["id"])

        for row in rows:
            if category := payload.get("category"):
                mark(
                    "category",
                    row,
                    row["profile_id"] != category["profile_id"]
                    or row["category_id"] != category["category_id"],
                )
            if tags := payload.get("tags"):
                current = {tag.casefold() for tag in row["tags"]}
                changed = any(item["name"].casefold() not in current for item in tags["add"])
                changed = changed or any(
                    item["name"].casefold() in current for item in tags["remove"]
                )
                mark("tags", row, changed)
            if merchant := payload.get("merchant"):
                target = merchant.get("id") if merchant["mode"] == "set" else None
                mark("merchant", row, row["merchant_id"] != target)
            for code, operation in payload.get("dimensions", {}).items():
                target = operation.get("value") if operation["mode"] == "set" else None
                mark(f"dimension:{code}", row, row["dimensions"].get(code) != target)
            if note := payload.get("note"):
                target = self._patched_note(row["note"], note["mode"], note["text"])
                mark("note", row, row["note"] != target)
            if status := payload.get("status"):
                mark("status", row, row["booking_status"] != status["value"])

        changed_ids = set().union(*affected.values()) if affected else set()
        field_changes = [
            {
                "field": field,
                "affected_count": len(transaction_ids),
                "unchanged_count": len(rows) - len(transaction_ids),
            }
            for field, transaction_ids in sorted(affected.items())
        ]
        warnings = [] if changed_ids else ["The selection contains no effective changes."]
        return {
            "changed_transaction_count": len(changed_ids),
            "field_changes": field_changes,
            "warnings": warnings,
        }

    @staticmethod
    def _validate_bulk_payload(
        connection: sqlite3.Connection,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if action == BulkAction.CATEGORIZE.value:
            profile = str(payload.get("profile") or "").strip()
            category = str(payload.get("category") or "").strip()
            row = connection.execute(
                """SELECT p.id AS profile_id, c.id AS category_id
                   FROM ledger_profiles p
                   JOIN ledger_profile_categories pc ON pc.profile_id=p.id
                   JOIN ledger_categories c ON c.id=pc.category_id
                   WHERE p.code=? AND c.code=?""",
                (profile, category),
            ).fetchone()
            if row is None:
                raise ValueError("Category does not belong to the selected profile")
            return {
                "profile": profile,
                "category": category,
                "profile_id": row["profile_id"],
                "category_id": row["category_id"],
            }
        if action in {BulkAction.TAG_ADD.value, BulkAction.TAG_REMOVE.value}:
            tag = str(payload.get("tag") or "").strip()
            row = connection.execute(
                "SELECT id, name FROM ledger_tags WHERE id=? OR name=? COLLATE NOCASE",
                (tag, tag),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown tag {tag}")
            return {"tag_id": row["id"], "tag": row["name"]}
        if action in {BulkAction.DIMENSION_SET.value, BulkAction.DIMENSION_CLEAR.value}:
            dimension = str(payload.get("dimension") or "").strip()
            row = connection.execute(
                "SELECT id, code FROM ledger_dimensions WHERE code=? COLLATE NOCASE",
                (dimension,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown dimension {dimension}")
            result = {"dimension_id": row["id"], "dimension": row["code"]}
            if action == BulkAction.DIMENSION_SET.value:
                value = str(payload.get("value") or "").strip()
                value_row = connection.execute(
                    """SELECT id, code FROM ledger_dimension_values
                       WHERE dimension_id=? AND code=? COLLATE NOCASE""",
                    (row["id"], value),
                ).fetchone()
                if value_row is None:
                    raise KeyError(f"Unknown dimension value {dimension}/{value}")
                result.update({"value_id": value_row["id"], "value": value_row["code"]})
            return result
        merchant = str(payload.get("merchant") or "").strip()
        row = connection.execute(
            """SELECT id, canonical_name FROM ledger_merchants
               WHERE id=? OR canonical_name=? COLLATE NOCASE""",
            (merchant, merchant),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown merchant {merchant}")
        return {"merchant_id": row["id"], "merchant": row["canonical_name"]}

    def execute_bulk_preview(
        self, preview_id: str, *, fingerprint: str | None = None
    ) -> dict[str, Any]:
        transaction_ids: list[str] = []
        changed_transaction_ids: list[str] = []
        with self.repository.database.connect() as connection:
            preview = connection.execute(
                "SELECT * FROM ledger_bulk_previews WHERE id=?", (preview_id,)
            ).fetchone()
            if preview is None:
                raise KeyError(f"Unknown bulk preview {preview_id}")
            action = preview["action"]
            if fingerprint is not None and fingerprint != preview["version_fingerprint"]:
                raise ValueError("Bulk preview fingerprint does not match")
            if action == BulkAction.EDIT_FIELDS.value and fingerprint is None:
                raise ValueError("Composite bulk edits require the preview fingerprint")
            item_rows = connection.execute(
                """SELECT bpi.transaction_id, bpi.expected_version,
                          t.version AS current_version
                   FROM ledger_bulk_preview_items bpi
                   JOIN ledger_transactions t ON t.id=bpi.transaction_id
                   WHERE bpi.preview_id=? ORDER BY bpi.transaction_id""",
                (preview_id,),
            ).fetchall()
            transaction_ids = [row["transaction_id"] for row in item_rows]
            if preview["status"] == "executed":
                result = {
                    "preview_id": preview_id,
                    "status": "executed",
                    "transaction_count": preview["transaction_count"],
                    "changed": 0,
                    "already_executed": True,
                }
            else:
                if preview["status"] != "prepared":
                    raise ValueError("Bulk preview is no longer executable")
                conflicts = [
                    row["transaction_id"]
                    for row in item_rows
                    if row["expected_version"] != row["current_version"]
                ]
                if conflicts:
                    raise VersionConflictError(
                        "Bulk preview is stale; changed transactions: " + ", ".join(conflicts),
                        transaction_ids=tuple(conflicts),
                    )
                payload = json.loads(preview["payload_json"])
                selection_rows = self._bulk_selection_rows(connection, tuple(transaction_ids))
                if action == BulkAction.EDIT_FIELDS.value:
                    revalidated = self._validate_edit_fields_payload(
                        connection,
                        self._raw_edit_fields_payload(payload),
                        selection_rows,
                    )
                    if revalidated != payload:
                        raise ValueError("Bulk edit configuration changed after preview")
                changed = 0
                versions: dict[str, int] = {}
                selected = {row["id"]: row for row in selection_rows}
                for item in item_rows:
                    if action == BulkAction.EDIT_FIELDS.value:
                        edit = self._apply_edit_fields(
                            connection,
                            payload,
                            selected[item["transaction_id"]],
                        )
                        did_change = bool(edit["changed"])
                    else:
                        edit = {"before": {}, "after": {}}
                        did_change = self._apply_bulk_action(
                            connection, action, payload, item["transaction_id"]
                        )
                    if did_change:
                        changed += 1
                        changed_transaction_ids.append(item["transaction_id"])
                        version = self._touch_transaction(connection, item["transaction_id"])
                        versions[item["transaction_id"]] = version
                        if transition := edit.get("status_transition"):
                            self._record_bulk_status_transition(
                                connection,
                                item["transaction_id"],
                                transition["from"],
                                transition["to"],
                                version,
                            )
                        connection.execute(
                            """UPDATE ledger_review_items
                               SET status='resolved', version=version+1,
                                   updated_at=CURRENT_TIMESTAMP
                               WHERE item_type='new_transaction'
                                 AND entity_type='transaction' AND entity_id=?
                                 AND status='open'""",
                            (item["transaction_id"],),
                        )
                        self.repository._audit(
                            connection,
                            "transaction",
                            item["transaction_id"],
                            f"bulk_{action}",
                            {
                                "version": item["expected_version"],
                                **edit["before"],
                            },
                            {
                                "payload": payload,
                                "version": version,
                                **edit["after"],
                            },
                            correlation_id=preview_id,
                        )
                    else:
                        versions[item["transaction_id"]] = item["current_version"]
                connection.execute(
                    """UPDATE ledger_bulk_previews
                       SET status='executed', executed_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (preview_id,),
                )
                self.repository._audit(
                    connection,
                    "bulk_preview",
                    preview_id,
                    "execute",
                    {"status": "prepared"},
                    {
                        "status": "executed",
                        "action": action,
                        "transaction_count": len(item_rows),
                        "changed": changed,
                    },
                    correlation_id=preview_id,
                )
                result = {
                    "preview_id": preview_id,
                    "status": "executed",
                    "transaction_count": len(item_rows),
                    "changed": changed,
                    "versions": versions,
                    "already_executed": False,
                }
        updated_ids = transaction_ids if result["already_executed"] else changed_transaction_ids
        result["updated_rows"] = [
            self.transaction_search_record(transaction_id) for transaction_id in updated_ids
        ]
        return result

    def _apply_edit_fields(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        status_transition: dict[str, str] | None = None

        if (category := payload.get("category")) and (
            row["profile_id"] != category["profile_id"]
            or row["category_id"] != category["category_id"]
        ):
            before["category"] = {
                "profile": row["classification_profile"],
                "category": row["category_code"],
            }
            connection.execute(
                """INSERT INTO ledger_classifications(
                       id, transaction_id, profile_id, category_id, source,
                       confidence, rationale, status, metadata_json
                   ) VALUES (?, ?, ?, ?, 'manual', 1.0,
                             'Sichere Mehrfachbearbeitung', 'applied', ?)""",
                (
                    self.repository._id("classification"),
                    row["id"],
                    category["profile_id"],
                    category["category_id"],
                    json.dumps({"bulk": True}, sort_keys=True),
                ),
            )
            after["category"] = {
                "profile": category["profile"],
                "category": category["category"],
            }

        if tags := payload.get("tags"):
            current = {tag.casefold() for tag in row["tags"]}
            added: list[str] = []
            removed: list[str] = []
            for item in tags["add"]:
                if item["name"].casefold() not in current:
                    connection.execute(
                        """INSERT INTO ledger_transaction_tags(transaction_id, tag_id)
                           VALUES (?, ?)""",
                        (row["id"], item["id"]),
                    )
                    added.append(item["name"])
            for item in tags["remove"]:
                if item["name"].casefold() in current:
                    connection.execute(
                        """DELETE FROM ledger_transaction_tags
                           WHERE transaction_id=? AND tag_id=?""",
                        (row["id"], item["id"]),
                    )
                    removed.append(item["name"])
            if added or removed:
                before["tags"] = row["tags"]
                after["tags"] = {"added": added, "removed": removed}

        if merchant := payload.get("merchant"):
            target = merchant.get("id") if merchant["mode"] == "set" else None
            if row["merchant_id"] != target:
                before["merchant"] = row["merchant"]
                connection.execute(
                    "UPDATE ledger_transactions SET merchant_id=? WHERE id=?",
                    (target, row["id"]),
                )
                after["merchant"] = merchant.get("value")

        for code, operation in payload.get("dimensions", {}).items():
            current_value = row["dimensions"].get(code)
            target_value = operation.get("value") if operation["mode"] == "set" else None
            if current_value == target_value:
                continue
            before.setdefault("dimensions", {})[code] = current_value
            if operation["mode"] == "clear":
                connection.execute(
                    """DELETE FROM ledger_transaction_dimensions
                       WHERE transaction_id=? AND dimension_id=?""",
                    (row["id"], operation["dimension_id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO ledger_transaction_dimensions(
                           transaction_id, dimension_id, dimension_value_id
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(transaction_id, dimension_id) DO UPDATE SET
                           dimension_value_id=excluded.dimension_value_id,
                           created_at=CURRENT_TIMESTAMP""",
                    (row["id"], operation["dimension_id"], operation["value_id"]),
                )
            after.setdefault("dimensions", {})[code] = target_value

        if note := payload.get("note"):
            target_note = self._patched_note(row["note"], note["mode"], note["text"])
            if target_note != row["note"]:
                before["note"] = row["note"]
                connection.execute(
                    "UPDATE ledger_transactions SET note=? WHERE id=?",
                    (target_note, row["id"]),
                )
                after["note"] = target_note

        if (status := payload.get("status")) and row["booking_status"] != status["value"]:
            before["booking_status"] = row["booking_status"]
            connection.execute(
                "UPDATE ledger_transactions SET booking_status=? WHERE id=?",
                (status["value"], row["id"]),
            )
            after["booking_status"] = status["value"]
            status_transition = {
                "from": row["booking_status"],
                "to": status["value"],
            }
        return {
            "changed": bool(after),
            "before": before,
            "after": after,
            "status_transition": status_transition,
        }

    def _record_bulk_status_transition(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        from_status: str,
        to_status: str,
        version: int,
    ) -> None:
        connection.execute(
            """INSERT INTO ledger_transaction_status_events(
                   id, transaction_id, from_status, to_status,
                   transaction_version, reason, source
               ) VALUES (?, ?, ?, ?, ?, 'Sichere Mehrfachbearbeitung', 'bulk')""",
            (
                self.repository._id("status-event"),
                transaction_id,
                from_status,
                to_status,
                version,
            ),
        )

    def _apply_bulk_action(
        self,
        connection: sqlite3.Connection,
        action: str,
        payload: dict[str, Any],
        transaction_id: str,
    ) -> bool:
        if action == BulkAction.CATEGORIZE.value:
            connection.execute(
                """INSERT INTO ledger_classifications(
                       id, transaction_id, profile_id, category_id, source,
                       confidence, rationale, status, metadata_json
                   ) VALUES (?, ?, ?, ?, 'manual', 1.0,
                             'Sichere Massenkategorisierung', 'applied', ?)""",
                (
                    self.repository._id("classification"),
                    transaction_id,
                    payload["profile_id"],
                    payload["category_id"],
                    json.dumps({"bulk": True}, sort_keys=True),
                ),
            )
            return True
        if action in {BulkAction.TAG_ADD.value, BulkAction.TAG_REMOVE.value}:
            exists = connection.execute(
                """SELECT 1 FROM ledger_transaction_tags
                   WHERE transaction_id=? AND tag_id=?""",
                (transaction_id, payload["tag_id"]),
            ).fetchone()
            if action == BulkAction.TAG_ADD.value:
                if exists:
                    return False
                connection.execute(
                    """INSERT INTO ledger_transaction_tags(transaction_id, tag_id)
                       VALUES (?, ?)""",
                    (transaction_id, payload["tag_id"]),
                )
                return True
            if not exists:
                return False
            connection.execute(
                """DELETE FROM ledger_transaction_tags
                   WHERE transaction_id=? AND tag_id=?""",
                (transaction_id, payload["tag_id"]),
            )
            return True
        if action in {BulkAction.DIMENSION_SET.value, BulkAction.DIMENSION_CLEAR.value}:
            current = connection.execute(
                """SELECT dimension_value_id FROM ledger_transaction_dimensions
                   WHERE transaction_id=? AND dimension_id=?""",
                (transaction_id, payload["dimension_id"]),
            ).fetchone()
            if action == BulkAction.DIMENSION_CLEAR.value:
                if not current:
                    return False
                connection.execute(
                    """DELETE FROM ledger_transaction_dimensions
                       WHERE transaction_id=? AND dimension_id=?""",
                    (transaction_id, payload["dimension_id"]),
                )
                return True
            if current and current["dimension_value_id"] == payload["value_id"]:
                return False
            connection.execute(
                """INSERT INTO ledger_transaction_dimensions(
                       transaction_id, dimension_id, dimension_value_id
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(transaction_id, dimension_id) DO UPDATE SET
                       dimension_value_id=excluded.dimension_value_id,
                       created_at=CURRENT_TIMESTAMP""",
                (transaction_id, payload["dimension_id"], payload["value_id"]),
            )
            return True
        current = connection.execute(
            "SELECT merchant_id FROM ledger_transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
        if current["merchant_id"] == payload["merchant_id"]:
            return False
        connection.execute(
            "UPDATE ledger_transactions SET merchant_id=? WHERE id=?",
            (payload["merchant_id"], transaction_id),
        )
        return True

    @staticmethod
    def _versioned_transaction(
        connection: sqlite3.Connection,
        transaction_id: str,
        expected_version: int,
    ) -> sqlite3.Row:
        transaction = connection.execute(
            "SELECT * FROM ledger_transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if transaction is None:
            raise KeyError(f"Unknown transaction {transaction_id}")
        if transaction["version"] != expected_version:
            raise VersionConflictError("The transaction has changed; reload it before writing.")
        if transaction["state"] == "void":
            raise ValueError("Void transactions cannot be changed")
        return transaction

    @staticmethod
    def _touch_transaction(connection: sqlite3.Connection, transaction_id: str) -> int:
        connection.execute(
            """UPDATE ledger_transactions
               SET version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (transaction_id,),
        )
        return int(
            connection.execute(
                "SELECT version FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()["version"]
        )

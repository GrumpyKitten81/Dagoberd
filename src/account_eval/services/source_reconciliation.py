"""Fingerprint-gated reconciliation of an imported source account."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from account_eval.ledger.repository import LedgerRepository


@dataclass(frozen=True, slots=True)
class SourceReconciliationPreview:
    source_account_id: str
    target_account_id: str
    mappings: tuple[dict[str, Any], ...]
    unmatched_source: tuple[dict[str, Any], ...]
    unmatched_target_count: int
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "matched_count": len(self.mappings),
            "unmatched_source_count": len(self.unmatched_source),
        }


class SourceReconciliationService:
    """Merge confirmed duplicate transactions while retaining every source row."""

    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository

    def preview(
        self, source_account_id: str, target_account_id: str
    ) -> SourceReconciliationPreview:
        if source_account_id == target_account_id:
            raise ValueError("Source and target account must differ")
        with self.repository.database.connect() as connection:
            accounts = connection.execute(
                "SELECT id, currency, version FROM ledger_accounts WHERE id IN (?, ?)",
                (source_account_id, target_account_id),
            ).fetchall()
            if len(accounts) != 2:
                raise KeyError("Source or target account does not exist")
            by_id = {row["id"]: row for row in accounts}
            if by_id[source_account_id]["currency"] != by_id[target_account_id]["currency"]:
                raise ValueError("Source and target account currencies differ")
            if connection.execute(
                """SELECT 1 FROM ledger_account_source_routes
                   WHERE source_account_id=? OR target_account_id=?""",
                (source_account_id, source_account_id),
            ).fetchone():
                raise ValueError("Source account already has a canonical-account route")
            if connection.execute(
                """SELECT 1 FROM ledger_account_source_routes
                   WHERE source_account_id=?""",
                (target_account_id,),
            ).fetchone():
                raise ValueError("Target account already routes to another account")
            source = self._transactions(connection, source_account_id)
            target = self._transactions(connection, target_account_id)
        used_sources: set[str] = set()
        used_targets: set[str] = set()
        mappings: list[dict[str, Any]] = []
        for key_index, match_kind in ((0, "valuta"), (1, "booking")):
            source_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            target_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            for transaction in source:
                if transaction["id"] not in used_sources:
                    source_groups.setdefault(self._keys(transaction)[key_index], []).append(
                        transaction
                    )
            for transaction in target:
                if transaction["id"] not in used_targets:
                    target_groups.setdefault(self._keys(transaction)[key_index], []).append(
                        transaction
                    )
            for key in sorted(source_groups, key=repr):
                source_rows = sorted(source_groups[key], key=lambda item: item["id"])
                target_rows = sorted(target_groups.get(key, []), key=lambda item: item["id"])
                for source_transaction, target_transaction in zip(
                    source_rows, target_rows, strict=False
                ):
                    used_sources.add(source_transaction["id"])
                    used_targets.add(target_transaction["id"])
                    mappings.append(
                        {
                            "source_transaction_id": source_transaction["id"],
                            "source_version": int(source_transaction["version"]),
                            "target_transaction_id": target_transaction["id"],
                            "target_version": int(target_transaction["version"]),
                            "match_kind": match_kind,
                        }
                    )
        unmatched = [
            {
                "transaction_id": source_transaction["id"],
                "version": int(source_transaction["version"]),
            }
            for source_transaction in source
            if source_transaction["id"] not in used_sources
        ]
        unmatched_target_ids = sorted(
            str(item["id"]) for item in target if item["id"] not in used_targets
        )
        payload = {
            "source_account_id": source_account_id,
            "target_account_id": target_account_id,
            "source_account_version": int(by_id[source_account_id]["version"]),
            "target_account_version": int(by_id[target_account_id]["version"]),
            "mappings": mappings,
            "unmatched_source": unmatched,
            "unmatched_target_ids": unmatched_target_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return SourceReconciliationPreview(
            source_account_id,
            target_account_id,
            tuple(mappings),
            tuple(unmatched),
            len(unmatched_target_ids),
            fingerprint,
        )

    def run(
        self,
        source_account_id: str,
        target_account_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        preview = self.preview(source_account_id, target_account_id)
        if preview.fingerprint != fingerprint:
            raise ValueError("Source reconciliation preview changed; create a new preview")
        backup = self.repository.database.backup()
        with self.repository.database.connect() as connection:
            for mapping in preview.mappings:
                self._merge_transaction(connection, mapping)
            for item in preview.unmatched_source:
                changed = connection.execute(
                    """UPDATE ledger_transactions SET account_id=?, version=version+1,
                           updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?""",
                    (
                        target_account_id,
                        item["transaction_id"],
                        item["version"],
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("Source transaction changed during reconciliation")
            connection.execute(
                """INSERT INTO ledger_account_source_routes(
                       source_account_id, target_account_id
                   ) VALUES (?, ?)""",
                (source_account_id, target_account_id),
            )
            routed_balance_snapshots = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_balance_snapshots WHERE account_id=?",
                    (source_account_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """UPDATE ledger_balance_snapshots SET account_id=?
                   WHERE account_id=?""",
                (target_account_id, source_account_id),
            )
            latest_balance = connection.execute(
                """SELECT balance_minor, balance_date FROM ledger_balance_snapshots
                   WHERE account_id=? ORDER BY balance_date DESC, created_at DESC LIMIT 1""",
                (target_account_id,),
            ).fetchone()
            if latest_balance:
                connection.execute(
                    """UPDATE ledger_accounts SET last_balance_minor=?, last_balance_at=?,
                           version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        latest_balance["balance_minor"],
                        latest_balance["balance_date"],
                        target_account_id,
                    ),
                )
            connection.execute(
                """UPDATE ledger_accounts SET enabled=0, last_balance_minor=NULL,
                       last_balance_at=NULL, version=version+1,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (source_account_id,),
            )
            self.repository._audit(
                connection,
                "account",
                source_account_id,
                "source_reconciliation",
                None,
                {
                    "target_account_id": target_account_id,
                    "matched": len(preview.mappings),
                    "moved": len(preview.unmatched_source),
                    "routed_balance_snapshots": routed_balance_snapshots,
                    "fingerprint": fingerprint,
                },
            )
        return {
            **preview.as_dict(),
            "backup": str(backup),
            "consolidated": True,
            "moved_unmatched": len(preview.unmatched_source),
        }

    @staticmethod
    def _transactions(connection: Any, account_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT id, booking_date, valuta_date, amount_minor, currency,
                          purpose, version
                   FROM ledger_transactions
                   WHERE account_id=? AND state='active'
                   ORDER BY booking_date, id""",
                (account_id,),
            )
        ]

    @classmethod
    def _keys(cls, transaction: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
        common = (
            int(transaction["amount_minor"]),
            transaction["currency"],
            re.sub(r"\s+", " ", str(transaction["purpose"]).casefold()).strip(),
        )
        valuta = transaction["valuta_date"] or transaction["booking_date"]
        return ((valuta, *common), (transaction["booking_date"], *common))

    def _merge_transaction(self, connection: Any, mapping: dict[str, Any]) -> None:
        source_id = mapping["source_transaction_id"]
        target_id = mapping["target_transaction_id"]
        source = connection.execute(
            "SELECT version FROM ledger_transactions WHERE id=?", (source_id,)
        ).fetchone()
        target = connection.execute(
            "SELECT version FROM ledger_transactions WHERE id=?", (target_id,)
        ).fetchone()
        if (
            source is None
            or target is None
            or int(source["version"]) != mapping["source_version"]
            or int(target["version"]) != mapping["target_version"]
        ):
            raise ValueError("Transaction changed during source reconciliation")
        relation = connection.execute(
            """SELECT 1 FROM ledger_transaction_relations
               WHERE source_transaction_id=? OR target_transaction_id=?""",
            (source_id, source_id),
        ).fetchone()
        if relation:
            raise ValueError("Source transaction has relations and cannot be reconciled")
        for match in connection.execute(
            "SELECT id, document_id FROM ledger_document_matches WHERE transaction_id=?",
            (source_id,),
        ).fetchall():
            existing = connection.execute(
                """SELECT 1 FROM ledger_document_matches
                   WHERE document_id=? AND transaction_id=?""",
                (match["document_id"], target_id),
            ).fetchone()
            if existing:
                connection.execute("DELETE FROM ledger_document_matches WHERE id=?", (match["id"],))
            else:
                connection.execute(
                    "UPDATE ledger_document_matches SET transaction_id=? WHERE id=?",
                    (target_id, match["id"]),
                )
        connection.execute(
            """UPDATE ledger_transaction_sources SET transaction_id=?, is_primary=0
               WHERE transaction_id=?""",
            (target_id, source_id),
        )
        connection.execute(
            "DELETE FROM ledger_review_items WHERE entity_type='transaction' AND entity_id=?",
            (source_id,),
        )
        for table in (
            "ledger_transaction_tags",
            "ledger_transaction_dimensions",
            "ledger_transaction_status_events",
            "ledger_classifications",
            "ledger_splits",
        ):
            connection.execute(f"DELETE FROM {table} WHERE transaction_id=?", (source_id,))
        connection.execute("DELETE FROM ledger_transactions WHERE id=?", (source_id,))
        self.repository._audit(
            connection,
            "transaction",
            target_id,
            "source_reconciliation",
            None,
            {
                "source_transaction_id": source_id,
                "match_kind": mapping["match_kind"],
            },
        )

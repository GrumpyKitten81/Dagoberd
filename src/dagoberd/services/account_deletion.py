"""Fingerprint-bound, local-only account deletion and reversible disable fallback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dagoberd.ledger.migrations import CURRENT_SCHEMA_VERSION
from dagoberd.ledger.repository import LedgerRepository
from dagoberd.reporting.analysis import AnalysisDefinition
from dagoberd.safe_files import prepared_new_file

CONTRACT = "dagoberd-account-deletion-v1"
# Every FK descendant must have an explicit policy; unknown descendants fail closed.
DELETABLE_TABLES = frozenset(
    {
        "ledger_accounts",
        "ledger_account_group_members",
        "ledger_account_field_options",
        "ledger_account_columns",
        "ledger_account_profiles",
        "ledger_account_stages",
        "ledger_rules",
        "ledger_hints",
        "ledger_transactions",
        "ledger_source_transactions",
        "ledger_source_transaction_revisions",
        "ledger_transaction_sources",
        "ledger_source_transaction_attributes",
        "ledger_transaction_status_events",
        "ledger_transaction_relations",
        "ledger_balance_snapshots",
        "ledger_reconciliations",
        "ledger_transaction_tags",
        "ledger_transaction_dimensions",
        "ledger_splits",
        "ledger_classifications",
        "ledger_document_matches",
        "ledger_bulk_preview_items",
    }
)
PROTECTED_TABLES = frozenset(
    {
        "ledger_account_source_routes",
        "ledger_import_previews",
        "ledger_import_runs",
        "ledger_sync_account_runs",
    }
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _contains(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(
            key in identifiers or _contains(item, identifiers) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains(item, identifiers) for item in value)
    return False


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(f'SELECT rowid AS _rowid_, * FROM "{table}" ORDER BY rowid')
    ]


def _graph(connection: sqlite3.Connection, account_id: str) -> dict[str, list[dict[str, Any]]]:
    """Follow actual foreign keys, including additional sources and split descendants."""
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    foreign_keys = {
        table: list(connection.execute(f'PRAGMA foreign_key_list("{table}")')) for table in tables
    }
    account = connection.execute(
        "SELECT rowid AS _rowid_, * FROM ledger_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if account is None:
        raise KeyError(f"Unknown account {account_id}")
    selected: dict[str, dict[int, dict[str, Any]]] = {
        "ledger_accounts": {account["_rowid_"]: dict(account)}
    }
    changed = True
    while changed:
        changed = False
        for table, keys in foreign_keys.items():
            for key in keys:
                parent, child_column, parent_column = key[2], key[3], key[4]
                parents = selected.get(parent, {})
                if parents and parent_column is None:
                    primary = [
                        row[1]
                        for row in connection.execute(f'PRAGMA table_info("{parent}")')
                        if row[5]
                    ]
                    if len(primary) != 1:
                        raise ValueError(
                            "Unsupported composite dependency; retain account by disabling it"
                        )
                    parent_column = primary[0]
                values = sorted(
                    {
                        row[parent_column]
                        for row in parents.values()
                        if row.get(parent_column) is not None
                    }
                )
                for offset in range(0, len(values), 400):
                    chunk = values[offset : offset + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in connection.execute(
                        f'SELECT rowid AS _rowid_, * FROM "{table}" '
                        f'WHERE "{child_column}" IN ({placeholders})',
                        chunk,
                    ):
                        found = selected.setdefault(table, {})
                        if row["_rowid_"] not in found:
                            found[row["_rowid_"]] = dict(row)
                            changed = True
        source_ids = {
            row["source_transaction_id"]
            for table in ("ledger_transactions", "ledger_transaction_sources", "ledger_splits")
            for row in selected.get(table, {}).values()
            if row.get("source_transaction_id") is not None
        }
        for source_id in sorted(source_ids):
            row = connection.execute(
                "SELECT rowid AS _rowid_, * FROM ledger_source_transactions WHERE id=?",
                (source_id,),
            ).fetchone()
            if row is not None:
                found = selected.setdefault("ledger_source_transactions", {})
                if row["_rowid_"] not in found:
                    found[row["_rowid_"]] = dict(row)
                    changed = True
    return {
        table: sorted(rows.values(), key=lambda row: row["_rowid_"])
        for table, rows in sorted(selected.items())
    }


class AccountDeletionService:
    def __init__(self, repository: LedgerRepository, metadata_dir: Path) -> None:
        self.repository = repository
        self.metadata_dir = metadata_dir

    def preview(
        self, account_id: str, *, cleanup_references: bool = False, fallback: str | None = None
    ) -> dict[str, Any]:
        if fallback not in (None, "disable"):
            raise ValueError("Unknown account deletion fallback")
        with closing(self.repository.database.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._plan(connection, account_id, cleanup_references, fallback)
            identifier = f"account-deletion-{uuid4().hex}"
            fingerprint = hashlib.sha256(_json(self._bound(plan)).encode()).hexdigest()
            connection.execute(
                """INSERT INTO ledger_account_deletions(
                       id, account_id, plan_json, fingerprint, status
                   ) VALUES (?, ?, ?, ?, 'prepared')""",
                (identifier, account_id, _json(self._bound(plan)), fingerprint),
            )
            public = self._public(plan)
            self.repository._audit(
                connection,
                "account_deletion",
                identifier,
                "preview",
                None,
                public,
                actor="cli",
                correlation_id=identifier,
            )
        return {"preview_id": identifier, "fingerprint": fingerprint, **public}

    @staticmethod
    def _public(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in plan.items() if key not in {"state", "state_fingerprint"}
        }

    @staticmethod
    def _bound(plan: dict[str, Any]) -> dict[str, Any]:
        # Retain a hash, not another copy of deleted financial payloads.
        return {
            **{key: value for key, value in plan.items() if key != "state"},
            "state_fingerprint": hashlib.sha256(_json(plan["state"]).encode()).hexdigest(),
        }

    def execute(self, preview_id: str, fingerprint: str) -> dict[str, Any]:
        with closing(self.repository.database.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = connection.execute(
                "SELECT * FROM ledger_account_deletions WHERE id=?", (preview_id,)
            ).fetchone()
            if stored is None:
                raise KeyError("Unknown account deletion preview")
            if stored["fingerprint"] != fingerprint:
                raise ValueError("Account deletion fingerprint does not match preview")
            original = json.loads(stored["plan_json"])
            if original["identity"] != self._identity(connection):
                raise ValueError("Account deletion preview belongs to another workspace or ledger")
            if stored["status"] == "executed":
                receipt = json.loads(stored["receipt_json"])
            else:
                plan = self._plan(
                    connection,
                    stored["account_id"],
                    original["cleanup_references"],
                    original["fallback"],
                )
                if hashlib.sha256(_json(self._bound(plan)).encode()).hexdigest() != fingerprint:
                    raise ValueError("Account deletion scope changed; create a new preview")
                if plan["action"] == "blocked":
                    raise ValueError(
                        "Account deletion is blocked; review blockers or use disable fallback"
                    )
                plan["correlation_id"] = preview_id
                self._apply(connection, plan)
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Account deletion would violate referential integrity")
                receipt = {
                    "contract_version": CONTRACT,
                    "preview_id": preview_id,
                    "account_id": stored["account_id"],
                    "fingerprint": fingerprint,
                    "action": plan["action"],
                    "changed": plan["action"] == "delete" or bool(plan["account"]["enabled"]),
                    "changes": plan["changes"],
                    "actor": "cli",
                    "executed_at": datetime.now(UTC).isoformat(),
                }
                connection.execute(
                    """UPDATE ledger_account_deletions SET status='executed', receipt_json=?,
                       executed_at=? WHERE id=?""",
                    (_json(receipt), receipt["executed_at"], preview_id),
                )
                self.repository._audit(
                    connection,
                    "account",
                    stored["account_id"],
                    f"deletion_{plan['action']}",
                    plan["account"],
                    receipt,
                    actor="cli",
                    correlation_id=preview_id,
                    idempotency_key=f"account-deletion:{preview_id}",
                )
        # The committed DB receipt is authoritative if publishing the file fails.
        return self._publish_receipt(receipt)

    def _publish_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        path = self.metadata_dir / "account-deletion-receipts" / f"{receipt['preview_id']}.json"
        try:
            if path.exists():
                if path.is_symlink() or path.read_text(encoding="utf-8") != _json(receipt) + "\n":
                    raise OSError("Receipt destination already contains different data")
            else:
                with prepared_new_file(path) as staged:
                    staged.write_text(_json(receipt) + "\n", encoding="utf-8")
            return {**receipt, "receipt": str(path)}
        except OSError:
            return {
                **receipt,
                "receipt": None,
                "warning": "Action committed; receipt is stored in the ledger. Retry the exact command to publish it.",
            }

    def _identity(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT workspace_id FROM ledger_workspace_identity WHERE singleton=1"
        ).fetchone()
        return {
            "workspace_id": row[0] if row else None,
            "database": str(self.repository.database.path.resolve()),
            "schema_version": CURRENT_SCHEMA_VERSION,
        }

    def _plan(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        cleanup: bool,
        fallback: str | None,
    ) -> dict[str, Any]:
        graph = _graph(connection, account_id)
        account = graph["ledger_accounts"][0]
        provider = dict(
            connection.execute(
                "SELECT * FROM ledger_providers WHERE id=?", (account["provider_id"],)
            ).fetchone()
        )
        blockers: set[str] = set()
        if account["enabled"]:
            blockers.add("account_enabled")
        for table in graph:
            if table in PROTECTED_TABLES:
                blockers.add(f"protected_history:{table}")
            elif table not in DELETABLE_TABLES:
                blockers.add(f"unsupported_dependency:{table}")
        transactions = graph.get("ledger_transactions", [])
        sources = graph.get("ledger_source_transactions", [])
        if transactions or sources:
            if provider["kind"] != "manual":
                blockers.add("imported_history")
            if any(row["account_id"] != account_id for row in transactions):
                blockers.add("shared_source_history")
            if any(
                row["booking_status"] != "reversed" and row["state"] != "void"
                for row in transactions
            ):
                blockers.add("non_reversed_transactions")
        evidence = []
        for transaction in transactions:
            audit = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ledger_audit_events WHERE entity_type='transaction' "
                    "AND entity_id=? AND operation='manual_create' AND actor='manual' ORDER BY id",
                    (transaction["id"],),
                )
            ]
            evidence.extend(audit)
            if not audit:
                blockers.add("unproven_manual_origin")
        primary_sources = {row["source_transaction_id"] for row in transactions}
        for source in sources:
            try:
                raw = json.loads(source["raw_payload_json"])
            except (ValueError, TypeError):
                raw = {}
            if (
                source["account_id"] != account_id
                or source["id"] not in primary_sources
                or not source["idempotency_key"].startswith("manual-source:")
                or not isinstance(raw, dict)
                or raw.get("origin") != "manual"
            ):
                blockers.add("unproven_manual_origin")
        if any(
            not row["idempotency_key"].startswith("manual-revision:")
            for row in graph.get("ledger_source_transaction_revisions", [])
        ):
            blockers.add("imported_revisions")
        if any(
            row["source"] not in ("manual", "manual-opening")
            for row in graph.get("ledger_balance_snapshots", [])
        ):
            blockers.add("imported_balances")
        transaction_ids = {row["id"] for row in transactions}
        if any(
            row["transaction_id"] not in transaction_ids for row in graph.get("ledger_splits", [])
        ):
            blockers.add("shared_split_history")
        for relation in graph.get("ledger_transaction_relations", []):
            if (
                relation["status"] == "confirmed"
                and {relation["source_transaction_id"], relation["target_transaction_id"]}
                - transaction_ids
            ):
                blockers.add("confirmed_cross_account_relation")
        if graph.get("ledger_document_matches") and not cleanup:
            blockers.add("document_links_require_cleanup")
        identifiers = {account_id} | {
            row["id"] for rows in graph.values() for row in rows if "id" in row
        }
        reviews = [
            row
            for row in _rows(connection, "ledger_review_items")
            if row["entity_id"] in identifiers
            or _contains(json.loads(row["metadata_json"]), identifiers)
        ]
        if any(row["status"] == "open" for row in reviews):
            blockers.add("open_review_items")
        if any(row["entity_id"] not in identifiers for row in reviews):
            blockers.add("shared_review_reference")
        graph["ledger_review_items"] = reviews
        if any(
            row["account_id"] != account_id
            for table in ("ledger_balance_snapshots", "ledger_reconciliations")
            for row in graph.get(table, [])
        ):
            blockers.add("shared_balance_history")
        extra: dict[str, Any] = {"provider": provider, "origin_evidence": evidence}
        analyses = []
        for row in _rows(connection, "ledger_saved_analyses"):
            definition = json.loads(row["definition_json"])
            if not _contains(definition, {account_id}):
                continue
            if not cleanup:
                blockers.add("analyses_require_cleanup")
            else:
                try:
                    parsed = AnalysisDefinition.model_validate(definition)
                    remaining = [
                        value for value in parsed.account_scope.account_ids if value != account_id
                    ]
                    if parsed.account_scope.mode != "selected" or not remaining:
                        blockers.add("empty_or_unsupported_analysis_scope")
                    else:
                        definition["account_scope"]["account_ids"] = remaining
                        AnalysisDefinition.model_validate(definition)
                except ValueError:
                    blockers.add("invalid_analysis_definition")
            analyses.append({"before": row, "after_definition": definition})
        extra["analyses"] = analyses
        groups = []
        for row in graph.get("ledger_account_group_members", []):
            groups.append(
                dict(
                    connection.execute(
                        "SELECT * FROM ledger_account_groups WHERE id=?", (row["group_id"],)
                    ).fetchone()
                )
            )
        extra["groups"] = groups
        # Operational JSON references have no FK. Keep historical snapshots, block live work.
        json_dependencies = {}
        for table, columns, active in (
            ("ledger_import_previews", ("summary_json",), None),
            ("ledger_sync_runs", ("account_ids_json", "cursor_json"), None),
            ("ledger_bulk_previews", ("payload_json",), {"prepared"}),
            (
                "ledger_llm_previews",
                ("transaction_versions_json", "payload_json"),
                {"ready", "running"},
            ),
        ):
            matches = []
            for row in _rows(connection, table):
                needles = identifiers | (
                    {account["external_id"]} if table == "ledger_import_previews" else set()
                )
                referenced = any(_contains(json.loads(row[column]), needles) for column in columns)
                if table == "ledger_bulk_previews":
                    referenced |= row["id"] in {
                        item["preview_id"] for item in graph.get("ledger_bulk_preview_items", [])
                    }
                if referenced:
                    matches.append(row)
                    if active is None or row["status"] in active:
                        blockers.add(f"operational_reference:{table}")
            json_dependencies[table] = matches
        extra["json_dependencies"] = json_dependencies
        action = "blocked" if blockers else "delete"
        if blockers and fallback == "disable":
            action = "disable"
        changes: dict[str, Any] = {}
        if action == "delete":
            changes = {
                "delete_rows": {table: len(rows) for table, rows in graph.items() if rows},
                "update_analyses": len(analyses),
                "update_groups": len(groups),
            }
        elif action == "disable":
            changes = {"disable_account": bool(account["enabled"])}
        dates = sorted(row["booking_date"] for row in transactions)
        return {
            "contract_version": CONTRACT,
            "identity": self._identity(connection),
            "account": {
                "id": account_id,
                "display_name": account["display_name"],
                "external_id_masked": "***",
                "provider_kind": provider["kind"],
                "currency": account["currency"],
                "version": account["version"],
                "enabled": bool(account["enabled"]),
            },
            "action": action,
            "cleanup_references": cleanup,
            "fallback": fallback,
            "blockers": sorted(blockers),
            "changes": changes,
            "operations": (
                [
                    {
                        "table": table,
                        "action": "delete",
                        "row_ids": [row.get("id", row["_rowid_"]) for row in rows],
                    }
                    for table, rows in graph.items()
                    if rows
                ]
                + [
                    {
                        "table": "ledger_saved_analyses",
                        "action": "remove_account",
                        "id": item["before"]["id"],
                        "version": item["before"]["version"],
                        "remaining_account_ids": item["after_definition"]["account_scope"][
                            "account_ids"
                        ],
                    }
                    for item in analyses
                ]
                if action == "delete"
                else []
            ),
            "guidance": (
                "Disable the account first, resolve open reviews and active work; "
                "retain imported history, or create a new preview with --fallback disable. "
                "Use --cleanup-references only after reviewing document and analysis changes."
            ),
            "dependencies": {table: len(rows) for table, rows in graph.items() if rows},
            "transactions": {
                "count": len(transactions),
                "booking_from": dates[0] if dates else None,
                "booking_to": dates[-1] if dates else None,
                "statuses": {
                    status: sum(row["booking_status"] == status for row in transactions)
                    for status in ("pending", "booked", "reversed", "corrected")
                },
                "source_count": len(sources),
            },
            "retained": ["audit", "historical_report_and_analysis_runs", "documents_and_files"],
            "state": {"graph": graph, **extra},
        }

    def _apply(self, connection: sqlite3.Connection, plan: dict[str, Any]) -> None:
        account_id = plan["account"]["id"]
        if plan["action"] == "disable":
            connection.execute(
                """UPDATE ledger_accounts SET enabled=0, version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND enabled<>0""",
                (account_id,),
            )
            return
        state = plan["state"]
        for analysis in state["analyses"]:
            connection.execute(
                """UPDATE ledger_saved_analyses SET definition_json=?, version=version+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (_json(analysis["after_definition"]), analysis["before"]["id"]),
            )
            self.repository._audit(
                connection,
                "saved_analysis",
                analysis["before"]["id"],
                "account_deletion_cleanup",
                analysis["before"],
                analysis["after_definition"],
                actor="cli",
                correlation_id=plan["correlation_id"],
            )
        for group in state["groups"]:
            connection.execute(
                "UPDATE ledger_account_groups SET version=version+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (group["id"],),
            )
            self.repository._audit(
                connection,
                "account_group",
                group["id"],
                "account_deletion_cleanup",
                {"version": group["version"], "removed_account_id": account_id},
                {"version": group["version"] + 1},
                actor="cli",
                correlation_id=plan["correlation_id"],
            )
        connection.execute("PRAGMA defer_foreign_keys=ON")
        for table, rows in state["graph"].items():
            for row in rows:
                connection.execute(f'DELETE FROM "{table}" WHERE rowid=?', (row["_rowid_"],))

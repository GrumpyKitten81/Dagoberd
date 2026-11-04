"""Ordered, checksummed SQLite schema migrations for the ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .llm_preview_migration import migrate_llm_preview_context
from .workspace_identity import migrate_workspace_identity


class MigrationError(RuntimeError):
    """Base error for schema migration failures."""


class MigrationChecksumError(MigrationError):
    """Raised when an applied migration no longer matches its definition."""


class DatabaseVersionError(MigrationError):
    """Raised when the database was created by a newer AccountEval version."""


MigrationFunction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    signature: str
    apply: MigrationFunction

    @property
    def checksum(self) -> str:
        payload = f"{self.version}:{self.name}:{self.signature}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL statements without sqlite3.executescript's implicit commit."""
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise MigrationError("Incomplete SQL statement in migration")


def _migration_001_legacy_metadata(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "ledger_splits",
        "source_transaction_id",
        "TEXT REFERENCES ledger_source_transactions(id)",
    )
    _add_column(
        connection,
        "ledger_splits",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _add_column(
        connection,
        "ledger_classifications",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )


def _migration_002_audit_context(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "ledger_audit_events",
        "actor",
        "TEXT NOT NULL DEFAULT 'system'",
    )
    _add_column(
        connection,
        "ledger_audit_events",
        "correlation_id",
        "TEXT",
    )
    _add_column(
        connection,
        "ledger_audit_events",
        "idempotency_key",
        "TEXT",
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
           ux_ledger_audit_events_idempotency_key
           ON ledger_audit_events(idempotency_key)
           WHERE idempotency_key IS NOT NULL"""
    )


def _migration_003_account_management(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "ledger_accounts",
        "enabled",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _add_column(
        connection,
        "ledger_accounts",
        "version",
        "INTEGER NOT NULL DEFAULT 1",
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_account_groups (
               id TEXT PRIMARY KEY,
               name TEXT NOT NULL UNIQUE,
               description TEXT NOT NULL DEFAULT '',
               version INTEGER NOT NULL DEFAULT 1,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_account_group_members (
               group_id TEXT NOT NULL REFERENCES ledger_account_groups(id)
                   ON DELETE CASCADE,
               account_id TEXT NOT NULL REFERENCES ledger_accounts(id)
                   ON DELETE CASCADE,
               PRIMARY KEY(group_id, account_id)
           )"""
    )


def _migration_004_sync_runs(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_sync_runs (
               id TEXT PRIMARY KEY,
               provider_id TEXT NOT NULL REFERENCES ledger_providers(id),
               status TEXT NOT NULL
                   CHECK(status IN ('running', 'completed', 'failed')),
               account_ids_json TEXT NOT NULL DEFAULT '[]',
               booking_from TEXT,
               booking_to TEXT,
               cursor_json TEXT NOT NULL DEFAULT '{}',
               imported_count INTEGER NOT NULL DEFAULT 0,
               skipped_count INTEGER NOT NULL DEFAULT 0,
               error_code TEXT,
               started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               finished_at TEXT
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS ix_ledger_sync_runs_provider_started
           ON ledger_sync_runs(provider_id, started_at DESC)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_sync_account_runs (
               sync_run_id TEXT NOT NULL REFERENCES ledger_sync_runs(id)
                   ON DELETE CASCADE,
               account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
               status TEXT NOT NULL
                   CHECK(status IN ('running', 'completed', 'failed')),
               cursor INTEGER NOT NULL DEFAULT 0,
               imported_count INTEGER NOT NULL DEFAULT 0,
               skipped_count INTEGER NOT NULL DEFAULT 0,
               error_code TEXT,
               started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               finished_at TEXT,
               PRIMARY KEY(sync_run_id, account_id)
           )"""
    )


def _migration_005_import_previews(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_import_previews (
               id TEXT PRIMARY KEY,
               source TEXT NOT NULL,
               file_path TEXT NOT NULL,
               file_hash TEXT NOT NULL,
               account_id TEXT REFERENCES ledger_accounts(id),
               status TEXT NOT NULL
                   CHECK(status IN ('prepared', 'executed', 'failed')),
               mapper_version TEXT NOT NULL,
               summary_json TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               executed_at TEXT
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_import_runs (
               id TEXT PRIMARY KEY,
               preview_id TEXT NOT NULL UNIQUE
                   REFERENCES ledger_import_previews(id),
               imported_count INTEGER NOT NULL,
               skipped_count INTEGER NOT NULL,
               account_count INTEGER NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _migration_006_review_workflows(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("pattern", "TEXT"),
        ("category_id", "TEXT REFERENCES ledger_categories(id)"),
        ("priority", "INTEGER NOT NULL DEFAULT 100"),
        ("active", "INTEGER NOT NULL DEFAULT 1"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _add_column(connection, "ledger_hints", column, definition)
    _add_column(connection, "ledger_classifications", "prompt_hash", "TEXT")
    _add_column(
        connection,
        "ledger_classifications",
        "version",
        "INTEGER NOT NULL DEFAULT 1",
    )
    for column, definition in (
        ("storage_path", "TEXT"),
        ("content_hash", "TEXT"),
        ("mime_type", "TEXT"),
        ("extraction_status", "TEXT NOT NULL DEFAULT 'metadata'"),
        ("status", "TEXT NOT NULL DEFAULT 'open'"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _add_column(connection, "ledger_documents", column, definition)
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_documents_content_hash
           ON ledger_documents(content_hash) WHERE content_hash IS NOT NULL"""
    )
    _add_column(
        connection,
        "ledger_document_matches",
        "version",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _add_column(
        connection,
        "ledger_document_matches",
        "updated_at",
        "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_report_runs (
               id TEXT PRIMARY KEY,
               profile_code TEXT,
               account_scope_json TEXT NOT NULL DEFAULT '{}',
               booking_from TEXT,
               booking_to TEXT,
               formats_json TEXT NOT NULL,
               output_paths_json TEXT NOT NULL,
               transaction_count INTEGER NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _migration_007_mcp_idempotency(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_mcp_requests (
               idempotency_key TEXT PRIMARY KEY,
               tool_name TEXT NOT NULL,
               result_json TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _migration_008_transaction_semantics(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "ledger_accounts",
        "last_balance_at",
        "TEXT",
    )
    _add_column(
        connection,
        "ledger_transactions",
        "booking_status",
        (
            "TEXT NOT NULL DEFAULT 'booked' CHECK(booking_status IN "
            "('pending', 'booked', 'reversed', 'corrected'))"
        ),
    )
    _add_column(
        connection,
        "ledger_sync_runs",
        "updated_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        connection,
        "ledger_sync_account_runs",
        "updated_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        connection,
        "ledger_import_runs",
        "updated_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_source_transaction_revisions (
            id TEXT PRIMARY KEY,
            source_transaction_id TEXT NOT NULL
                REFERENCES ledger_source_transactions(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            raw_payload_json TEXT NOT NULL,
            normalized_payload_json TEXT NOT NULL,
            booking_status TEXT NOT NULL CHECK(booking_status IN
                ('pending', 'booked', 'reversed', 'corrected')),
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_source_revisions_source
        ON ledger_source_transaction_revisions(
            source_transaction_id, imported_at
        );

        CREATE TABLE IF NOT EXISTS ledger_transaction_status_events (
            id TEXT PRIMARY KEY,
            transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
            from_status TEXT CHECK(from_status IS NULL OR from_status IN
                ('pending', 'booked', 'reversed', 'corrected')),
            to_status TEXT NOT NULL CHECK(to_status IN
                ('pending', 'booked', 'reversed', 'corrected')),
            transaction_version INTEGER NOT NULL,
            reason TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS
            ix_ledger_transaction_status_events_transaction
        ON ledger_transaction_status_events(transaction_id, created_at);

        CREATE TABLE IF NOT EXISTS ledger_transaction_relations (
            id TEXT PRIMARY KEY,
            relation_type TEXT NOT NULL CHECK(relation_type IN
                ('transfer', 'refund', 'reversal', 'correction')),
            source_transaction_id TEXT NOT NULL
                REFERENCES ledger_transactions(id),
            target_transaction_id TEXT NOT NULL
                REFERENCES ledger_transactions(id),
            status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN
                ('suggested', 'confirmed', 'rejected', 'resolved')),
            score REAL NOT NULL DEFAULT 1.0
                CHECK(score >= 0.0 AND score <= 1.0),
            reasons_json TEXT NOT NULL DEFAULT '[]',
            fee_amount_minor INTEGER NOT NULL DEFAULT 0
                CHECK(fee_amount_minor >= 0),
            source_version INTEGER NOT NULL,
            target_version INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(source_transaction_id <> target_transaction_id),
            UNIQUE(
                relation_type,
                source_transaction_id,
                target_transaction_id
            )
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_status
        ON ledger_transaction_relations(status, relation_type, updated_at);
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_source
        ON ledger_transaction_relations(source_transaction_id, status);
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_relations_target
        ON ledger_transaction_relations(target_transaction_id, status);

        CREATE TABLE IF NOT EXISTS ledger_balance_snapshots (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
            balance_date TEXT NOT NULL,
            balance_minor INTEGER NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            external_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_balance_snapshots_account_date
        ON ledger_balance_snapshots(account_id, balance_date, created_at);

        CREATE TABLE IF NOT EXISTS ledger_reconciliations (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
            snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES ledger_balance_snapshots(id),
            previous_snapshot_id TEXT REFERENCES ledger_balance_snapshots(id),
            calculated_balance_minor INTEGER,
            difference_minor INTEGER,
            transaction_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN
                ('baseline', 'balanced', 'difference')),
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_reconciliations_account
        ON ledger_reconciliations(account_id, created_at);
        """,
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS ix_ledger_transactions_booking_status
           ON ledger_transactions(booking_status, booking_date)"""
    )

    for row in connection.execute(
        """SELECT st.id AS source_id, st.raw_payload_json,
                  t.booking_date, t.valuta_date, t.amount_minor, t.currency,
                  t.purpose, t.counterparty, t.reference, t.booking_status
           FROM ledger_source_transactions st
           JOIN ledger_transactions t ON t.source_transaction_id=st.id"""
    ).fetchall():
        normalized = json.dumps(
            {
                "booking_date": row["booking_date"],
                "valuta_date": row["valuta_date"],
                "amount_minor": row["amount_minor"],
                "currency": row["currency"],
                "purpose": row["purpose"],
                "counterparty": row["counterparty"],
                "reference": row["reference"],
                "booking_status": row["booking_status"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        revision_key = hashlib.sha256(
            (f"migration-8|{row['source_id']}|{row['raw_payload_json']}|{normalized}").encode()
        ).hexdigest()
        connection.execute(
            """INSERT OR IGNORE INTO ledger_source_transaction_revisions(
                   id, source_transaction_id, idempotency_key,
                   raw_payload_json, normalized_payload_json, booking_status
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"source-revision-initial-{row['source_id']}",
                row["source_id"],
                revision_key,
                row["raw_payload_json"],
                normalized,
                row["booking_status"],
            ),
        )
    connection.execute(
        """INSERT OR IGNORE INTO ledger_transaction_status_events(
               id, transaction_id, from_status, to_status,
               transaction_version, reason, source
           )
           SELECT 'status-event-initial-' || id, id, NULL, booking_status, 1,
                  'Vor V2 vorhandener Buchungsstatus', 'migration'
           FROM ledger_transactions"""
    )
    connection.execute(
        """UPDATE ledger_accounts
           SET last_balance_at=COALESCE(last_balance_at, updated_at)
           WHERE last_balance_minor IS NOT NULL"""
    )


def _migration_009_personal_organization(
    connection: sqlite3.Connection,
) -> None:
    _add_column(
        connection,
        "ledger_accounts",
        "account_type",
        (
            "TEXT NOT NULL DEFAULT 'checking' CHECK(account_type IN "
            "('checking', 'credit_card', 'savings', 'cash', 'loan', "
            "'asset', 'liability'))"
        ),
    )
    _add_column(
        connection,
        "ledger_transactions",
        "merchant_id",
        "TEXT REFERENCES ledger_merchants(id)",
    )
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_merchants (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger_merchant_aliases (
            id TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL
                REFERENCES ledger_merchants(id) ON DELETE CASCADE,
            normalized_alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_alias TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_transactions_merchant
        ON ledger_transactions(merchant_id, booking_date);

        CREATE TABLE IF NOT EXISTS ledger_tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger_transaction_tags (
            transaction_id TEXT NOT NULL
                REFERENCES ledger_transactions(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES ledger_tags(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(transaction_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_tags_tag
        ON ledger_transaction_tags(tag_id, transaction_id);

        CREATE TABLE IF NOT EXISTS ledger_dimensions (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL COLLATE NOCASE UNIQUE,
            name TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger_dimension_values (
            id TEXT PRIMARY KEY,
            dimension_id TEXT NOT NULL
                REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
            code TEXT NOT NULL COLLATE NOCASE,
            name TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(dimension_id, code)
        );
        CREATE TABLE IF NOT EXISTS ledger_transaction_dimensions (
            transaction_id TEXT NOT NULL
                REFERENCES ledger_transactions(id) ON DELETE CASCADE,
            dimension_id TEXT NOT NULL
                REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
            dimension_value_id TEXT NOT NULL
                REFERENCES ledger_dimension_values(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(transaction_id, dimension_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_dimensions_value
        ON ledger_transaction_dimensions(dimension_value_id, transaction_id);

        CREATE TABLE IF NOT EXISTS ledger_review_items (
            id TEXT PRIMARY KEY,
            item_type TEXT NOT NULL CHECK(item_type IN (
                'new_transaction', 'pending_transaction',
                'classification_suggestion', 'relation_suggestion',
                'document_match', 'unmatched_document',
                'reconciliation_difference', 'duplicate_candidate',
                'import_warning', 'processing_error'
            )),
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50
                CHECK(priority BETWEEN 0 AND 100),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'resolved', 'dismissed')),
            version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_review_items_open_entity
        ON ledger_review_items(item_type, entity_type, entity_id)
        WHERE status='open';
        CREATE INDEX IF NOT EXISTS ix_ledger_review_items_status
        ON ledger_review_items(status, priority DESC, created_at DESC);

        CREATE TABLE IF NOT EXISTS ledger_bulk_previews (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL CHECK(action IN (
                'categorize', 'tag_add', 'tag_remove', 'dimension_set',
                'dimension_clear', 'merchant_set'
            )),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'prepared'
                CHECK(status IN ('prepared', 'executed', 'expired')),
            transaction_count INTEGER NOT NULL,
            version_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            executed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ledger_bulk_preview_items (
            preview_id TEXT NOT NULL
                REFERENCES ledger_bulk_previews(id) ON DELETE CASCADE,
            transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
            expected_version INTEGER NOT NULL,
            PRIMARY KEY(preview_id, transaction_id)
        );
        """,
    )
    for code, name in (
        ("person", "Person"),
        ("household", "Haushalt"),
        ("property", "Immobilie"),
        ("project", "Projekt"),
    ):
        connection.execute(
            """INSERT OR IGNORE INTO ledger_dimensions(id, code, name)
               VALUES (?, ?, ?)""",
            (f"builtin-dimension-{code}", code, name),
        )
    connection.execute(
        """INSERT OR IGNORE INTO ledger_review_items(
               id, item_type, entity_type, entity_id, reason, priority
           )
           SELECT 'review-migration-new-' || t.id, 'new_transaction',
                  'transaction', t.id,
                  'Bestehende Buchung noch nicht im V2-Posteingang geprüft',
                  40
           FROM ledger_transactions t
           WHERE t.state<>'void'"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO ledger_review_items(
               id, item_type, entity_type, entity_id, reason, priority
           )
           SELECT 'review-migration-pending-' || t.id,
                  'pending_transaction', 'transaction', t.id,
                  'Vorgemerkte Buchung benötigt Abschlussprüfung', 80
           FROM ledger_transactions t
           WHERE t.booking_status='pending' AND t.state<>'void'"""
    )


def _migration_010_personalized_analysis(
    connection: sqlite3.Connection,
) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_saved_analyses (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL COLLATE NOCASE UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            definition_json TEXT NOT NULL,
            contract_version TEXT NOT NULL
                DEFAULT 'account-eval-analysis-v1',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger_exchange_rates (
            id TEXT PRIMARY KEY,
            source_currency TEXT NOT NULL,
            target_currency TEXT NOT NULL,
            rate_decimal TEXT NOT NULL,
            rate_date TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(length(source_currency)=3),
            CHECK(length(target_currency)=3),
            CHECK(source_currency<>target_currency),
            UNIQUE(source_currency, target_currency, rate_date, source,
                   source_version)
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_exchange_rates_lookup
        ON ledger_exchange_rates(
            source_currency, target_currency, rate_date DESC
        );
        CREATE TABLE IF NOT EXISTS ledger_analysis_runs (
            id TEXT PRIMARY KEY,
            saved_analysis_id TEXT REFERENCES ledger_saved_analyses(id),
            definition_version INTEGER NOT NULL,
            definition_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            result_hash TEXT NOT NULL,
            output_paths_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_analysis_runs_saved
        ON ledger_analysis_runs(saved_analysis_id, created_at DESC);
        """,
    )


def _migration_011_llm_previews(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_llm_previews (
            id TEXT PRIMARY KEY,
            profile_code TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('local', 'cloud')),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            transaction_versions_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready', 'running', 'executed', 'failed', 'expired', 'invalidated')),
            result_json TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            executed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_llm_previews_status
        ON ledger_llm_previews(status, expires_at);
        """,
    )


def _migration_012_report_delivery(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_delivery_previews (
            id TEXT PRIMARY KEY,
            report_run_id TEXT NOT NULL REFERENCES ledger_report_runs(id),
            target_name TEXT NOT NULL,
            recipients_json TEXT NOT NULL,
            message_json TEXT NOT NULL,
            artifacts_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready', 'sent', 'expired', 'invalidated')),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ledger_service_runs (
            id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_delivery_previews_status
        ON ledger_delivery_previews(status, expires_at);
        """,
    )


def _migration_013_historical_transfer_classifications(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE ledger_classifications RENAME TO ledger_classifications_previous"
    )

    _execute_script(
        connection,
        """
        CREATE TABLE ledger_classifications (
            id TEXT PRIMARY KEY,
            transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
            profile_id TEXT NOT NULL REFERENCES ledger_profiles(id),
            category_id TEXT REFERENCES ledger_categories(id),
            source TEXT NOT NULL CHECK(source IN
                ('rule', 'history', 'hint', 'llm', 'manual', 'historical_transfer')),
            confidence REAL NOT NULL,
            rationale TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('applied', 'suggested', 'rejected')),
            model_version TEXT,
            prompt_hash TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO ledger_classifications(
            id, transaction_id, profile_id, category_id, source, confidence,
            rationale, status, model_version, prompt_hash, version, metadata_json, created_at
        ) SELECT
            id, transaction_id, profile_id, category_id, source, confidence,
            rationale, status, model_version, prompt_hash, version, metadata_json, created_at
        FROM ledger_classifications_previous;
        DROP TABLE ledger_classifications_previous;
        CREATE INDEX ix_ledger_classifications_transaction
        ON ledger_classifications(transaction_id, profile_id, created_at DESC);
        """,
    )


def _migration_014_gui_editing(connection: sqlite3.Connection) -> None:
    _add_column(connection, "ledger_transactions", "note", "TEXT NOT NULL DEFAULT ''")
    # DESIGN: LedgerDatabase.initialize reapplies schema.sql and recreates the view.
    _execute_script(
        connection,
        """CREATE TABLE IF NOT EXISTS ledger_account_field_options (
               account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
               field_kind TEXT NOT NULL CHECK(field_kind IN ('category', 'tag', 'dimension_value')),
               field_code TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY(account_id, field_kind, field_code)
           );""",
    )


def _migration_015_classification_config(connection: sqlite3.Connection) -> None:
    _add_column(connection, "ledger_dimensions", "active", "INTEGER NOT NULL DEFAULT 1")
    _add_column(connection, "ledger_dimension_values", "active", "INTEGER NOT NULL DEFAULT 1")
    _add_column(connection, "ledger_rules", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column(connection, "ledger_rules", "account_id", "TEXT REFERENCES ledger_accounts(id)")
    _add_column(connection, "ledger_hints", "account_id", "TEXT REFERENCES ledger_accounts(id)")
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_account_columns (
            account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
            dimension_id TEXT NOT NULL REFERENCES ledger_dimensions(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY(account_id, dimension_id)
        );
        CREATE TABLE IF NOT EXISTS ledger_account_profiles (
            account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES ledger_profiles(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY(account_id, profile_id)
        );
        CREATE TABLE IF NOT EXISTS ledger_account_stages (
            account_id TEXT NOT NULL REFERENCES ledger_accounts(id) ON DELETE CASCADE,
            stage TEXT NOT NULL CHECK(stage IN ('rule', 'history', 'hint', 'llm')),
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(account_id, stage)
        );
        """,
    )


def _migration_016_source_provenance_and_document_batches(
    connection: sqlite3.Connection,
) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_transaction_sources (
            transaction_id TEXT NOT NULL
                REFERENCES ledger_transactions(id) ON DELETE CASCADE,
            source_transaction_id TEXT NOT NULL UNIQUE
                REFERENCES ledger_source_transactions(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
            linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(transaction_id, source_transaction_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_transaction_sources_primary
        ON ledger_transaction_sources(transaction_id) WHERE is_primary=1;
        CREATE INDEX IF NOT EXISTS ix_ledger_transaction_sources_transaction
        ON ledger_transaction_sources(transaction_id, linked_at);

        CREATE TABLE IF NOT EXISTS ledger_source_transaction_attributes (
            source_transaction_id TEXT NOT NULL
                REFERENCES ledger_source_transactions(id) ON DELETE CASCADE,
            attribute_key TEXT NOT NULL COLLATE NOCASE,
            attribute_value TEXT NOT NULL,
            value_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(source_transaction_id, attribute_key)
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_source_attributes_key_value
        ON ledger_source_transaction_attributes(
            attribute_key, attribute_value COLLATE NOCASE
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_source_attributes_value
        ON ledger_source_transaction_attributes(attribute_value COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS ledger_document_origins (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL
                REFERENCES ledger_documents(id) ON DELETE CASCADE,
            origin_key TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            mtime_ns INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_document_origins_document
        ON ledger_document_origins(document_id, created_at);
        CREATE INDEX IF NOT EXISTS ix_ledger_document_origins_content_hash
        ON ledger_document_origins(content_hash);

        CREATE TABLE IF NOT EXISTS ledger_document_batch_previews (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            source_root TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'prepared'
                CHECK(status IN ('prepared', 'executed', 'failed')),
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            executed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_document_batch_previews_status
        ON ledger_document_batch_previews(status, created_at DESC);
        """,
    )
    connection.execute(
        """INSERT OR IGNORE INTO ledger_transaction_sources(
               transaction_id, source_transaction_id, is_primary
           )
           SELECT id, source_transaction_id, 1 FROM ledger_transactions"""
    )


def _migration_017_composite_bulk_edits(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE ledger_bulk_previews_next (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL CHECK(action IN (
                'categorize', 'tag_add', 'tag_remove', 'dimension_set',
                'dimension_clear', 'merchant_set', 'edit_fields'
            )),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'prepared'
                CHECK(status IN ('prepared', 'executed', 'expired')),
            transaction_count INTEGER NOT NULL,
            version_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            executed_at TEXT
        );
        INSERT INTO ledger_bulk_previews_next(
            id, action, payload_json, status, transaction_count,
            version_fingerprint, created_at, executed_at
        ) SELECT
            id, action, payload_json, status, transaction_count,
            version_fingerprint, created_at, executed_at
        FROM ledger_bulk_previews;
        CREATE TABLE ledger_bulk_preview_items_next (
            preview_id TEXT NOT NULL
                REFERENCES ledger_bulk_previews_next(id) ON DELETE CASCADE,
            transaction_id TEXT NOT NULL REFERENCES ledger_transactions(id),
            expected_version INTEGER NOT NULL,
            PRIMARY KEY(preview_id, transaction_id)
        );
        INSERT INTO ledger_bulk_preview_items_next(
            preview_id, transaction_id, expected_version
        ) SELECT preview_id, transaction_id, expected_version
          FROM ledger_bulk_preview_items;
        DROP TABLE ledger_bulk_preview_items;
        DROP TABLE ledger_bulk_previews;
        ALTER TABLE ledger_bulk_previews_next RENAME TO ledger_bulk_previews;
        ALTER TABLE ledger_bulk_preview_items_next RENAME TO ledger_bulk_preview_items;
        """,
    )


def _migration_018_account_source_routes(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS ledger_account_source_routes (
            source_account_id TEXT PRIMARY KEY REFERENCES ledger_accounts(id) ON DELETE CASCADE,
            target_account_id TEXT NOT NULL REFERENCES ledger_accounts(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(source_account_id <> target_account_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ledger_account_source_routes_target
        ON ledger_account_source_routes(target_account_id);
        """,
    )


MIGRATIONS = (
    Migration(
        version=1,
        name="legacy_metadata",
        signature=(
            "ledger_splits.source_transaction_id:text-fk;"
            "ledger_splits.metadata_json:text-not-null-default-object;"
            "ledger_classifications.metadata_json:text-not-null-default-object"
        ),
        apply=_migration_001_legacy_metadata,
    ),
    Migration(
        version=2,
        name="audit_context",
        signature=(
            "ledger_audit_events.actor:text-not-null-default-system;"
            "ledger_audit_events.correlation_id:text;"
            "ledger_audit_events.idempotency_key:text;"
            "unique-partial-index:idempotency_key"
        ),
        apply=_migration_002_audit_context,
    ),
    Migration(
        version=3,
        name="account_management",
        signature=(
            "ledger_accounts.enabled:integer-not-null-default-1;"
            "ledger_accounts.version:integer-not-null-default-1;"
            "ledger_account_groups:v1;"
            "ledger_account_group_members:v1"
        ),
        apply=_migration_003_account_management,
    ),
    Migration(
        version=4,
        name="sync_runs",
        signature=("ledger_sync_runs:v1;index:provider-started;ledger_sync_account_runs:v1"),
        apply=_migration_004_sync_runs,
    ),
    Migration(
        version=5,
        name="import_previews",
        signature="ledger_import_previews:v1;ledger_import_runs:v1",
        apply=_migration_005_import_previews,
    ),
    Migration(
        version=6,
        name="review_workflows",
        signature=(
            "ledger_hints:deterministic-v2;"
            "ledger_classifications:review-v2;"
            "ledger_documents:local-storage-v2;"
            "ledger_document_matches:review-v2;ledger_report_runs:v1"
        ),
        apply=_migration_006_review_workflows,
    ),
    Migration(
        version=7,
        name="mcp_idempotency",
        signature="ledger_mcp_requests:v1",
        apply=_migration_007_mcp_idempotency,
    ),
    Migration(
        version=8,
        name="transaction_semantics",
        signature=(
            "ledger_transactions.booking_status:v1;"
            "ledger_source_transaction_revisions:v1;"
            "ledger_transaction_status_events:v2;"
            "ledger_transaction_relations:v1;"
            "ledger_balance_snapshots:v1;ledger_reconciliations:v2;"
            "ledger_accounts.last_balance_at:v1;sync.updated_count:v1"
            ";import.updated_count:v1"
        ),
        apply=_migration_008_transaction_semantics,
    ),
    Migration(
        version=9,
        name="personal_organization",
        signature=(
            "ledger_accounts.account_type:v1;"
            "ledger_transactions.merchant_id:v1;"
            "ledger_merchants:v1;ledger_merchant_aliases:v1;"
            "ledger_tags:v1;ledger_transaction_tags:v1;"
            "ledger_dimensions:v1;ledger_dimension_values:v1;"
            "ledger_transaction_dimensions:v1;ledger_review_items:v1;"
            "ledger_bulk_previews:v1;ledger_bulk_preview_items:v1;"
            "builtin-dimensions:v1;review-backfill:v1"
        ),
        apply=_migration_009_personal_organization,
    ),
    Migration(
        version=10,
        name="personalized_analysis",
        signature=(
            "ledger_saved_analyses:v1;ledger_exchange_rates:v1;"
            "ledger_analysis_runs:v1;analysis-contract:v1"
        ),
        apply=_migration_010_personalized_analysis,
    ),
    Migration(
        version=11,
        name="llm_preview_gate",
        signature="ledger_llm_previews:v2;redacted-payload-fingerprint:v1",
        apply=_migration_011_llm_previews,
    ),
    Migration(
        version=12,
        name="report_delivery",
        signature="ledger_delivery_previews:v2;ledger_service_runs:v1",
        apply=_migration_012_report_delivery,
    ),
    Migration(
        version=13,
        name="historical_transfer_classifications",
        signature="ledger_classifications.source:historical-transfer-v1",
        apply=_migration_013_historical_transfer_classifications,
    ),
    Migration(
        version=14,
        name="gui_editing",
        signature="ledger_transactions.note:text-not-null-default-empty;ledger_account_field_options:v1",
        apply=_migration_014_gui_editing,
    ),
    Migration(
        version=15,
        name="classification_config",
        signature=(
            "ledger_dimensions.active:v1;ledger_dimension_values.active:v1;"
            "ledger_account_columns:v1;ledger_rules.version:v1;ledger_rules.account_id:v1;"
            "ledger_hints.account_id:v1;ledger_account_profiles:v1;ledger_account_stages:v1"
        ),
        apply=_migration_015_classification_config,
    ),
    Migration(
        version=16,
        name="source_provenance_and_document_batches",
        signature=(
            "ledger_transaction_sources:v1;primary-source-backfill:v1;"
            "ledger_source_transaction_attributes:v1;indexed-key-value:v1;"
            "ledger_document_origins:v1;ledger_document_batch_previews:v1"
        ),
        apply=_migration_016_source_provenance_and_document_batches,
    ),
    Migration(
        version=17,
        name="composite_bulk_edits",
        signature="ledger_bulk_previews.action:edit-fields-v1",
        apply=_migration_017_composite_bulk_edits,
    ),
    Migration(
        version=18,
        name="account_source_routes",
        signature="ledger_account_source_routes:v1;source-to-canonical-account:v1",
        apply=_migration_018_account_source_routes,
    ),
    Migration(
        version=19,
        name="llm_preview_request_context",
        signature="ledger_llm_previews.request_context_json:v1;invalidate-unbound-previews:v1",
        apply=migrate_llm_preview_context,
    ),
    Migration(
        version=20,
        name="workspace_identity",
        signature="ledger_workspace_identity:v1",
        apply=migrate_workspace_identity,
    ),
)

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               checksum TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def applied_migrations(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    ensure_migration_table(connection)
    return {
        int(row["version"]): row
        for row in connection.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations"
        )
    }


def pending_migrations(connection: sqlite3.Connection) -> tuple[Migration, ...]:
    applied = applied_migrations(connection)
    known = {migration.version: migration for migration in MIGRATIONS}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise DatabaseVersionError(
            "Database contains migrations newer than this AccountEval version: "
            + ", ".join(str(version) for version in unknown)
        )
    for version, row in applied.items():
        migration = known[version]
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationChecksumError(
                f"Migration {version} ({migration.name}) checksum mismatch"
            )
    return tuple(migration for migration in MIGRATIONS if migration.version not in applied)


def apply_pending_migrations(
    connection: sqlite3.Connection,
) -> tuple[Migration, ...]:
    pending = pending_migrations(connection)
    for migration in pending:
        savepoint = f"account_eval_migration_{migration.version}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            migration.apply(connection)
            connection.execute(
                """INSERT INTO schema_migrations(version, name, checksum)
                   VALUES (?, ?, ?)""",
                (migration.version, migration.name, migration.checksum),
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
    return pending

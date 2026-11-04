import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from account_eval.cli import main
from account_eval.ledger.database import LedgerDatabase
from account_eval.ledger.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationChecksumError,
)
from account_eval.ledger.models import NormalizedTransaction
from account_eval.ledger.repository import LedgerRepository


def test_schema_migrations_are_ordered_and_idempotent(tmp_path):
    database = LedgerDatabase(tmp_path / "ledger.db")

    database.initialize()
    with database.connect() as connection:
        first = connection.execute(
            """SELECT version, name, checksum, applied_at
               FROM schema_migrations ORDER BY version"""
        ).fetchall()

    database.initialize()
    with database.connect() as connection:
        second = connection.execute(
            """SELECT version, name, checksum, applied_at
               FROM schema_migrations ORDER BY version"""
        ).fetchall()

    assert [row["version"] for row in first] == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert [tuple(row) for row in second] == [tuple(row) for row in first]


def test_v18_upgrade_preserves_suggestions_and_audit_but_invalidates_unbound_previews(tmp_path):
    database = LedgerDatabase(tmp_path / "v18.db")
    repository = LedgerRepository(database)
    repository.import_transactions(
        "file",
        "Test",
        [
            NormalizedTransaction(
                "source", date(2026, 7, 1), Decimal("-10"), "EUR", "Test", "account"
            ),
        ],
    )
    transaction = repository.list_transactions()[0]
    identifier = repository.classify(
        transaction["id"],
        "test",
        "expense.test",
        "llm",
        0.8,
        "Historic rationale",
        status="suggested",
        model_version="historic/model",
        prompt_hash="historic-prompt",
        metadata={"taxonomy_version": "historic-taxonomy"},
    )
    with database.connect() as connection:
        audit_before = [
            tuple(row) for row in connection.execute("SELECT * FROM ledger_audit_events")
        ]
        classification_before = tuple(
            connection.execute(
                "SELECT * FROM ledger_classifications WHERE id=?",
                (identifier,),
            ).fetchone()
        )
        connection.execute("ALTER TABLE ledger_llm_previews DROP COLUMN request_context_json")
        connection.execute("DELETE FROM schema_migrations WHERE version=19")
        for status in ("ready", "running", "executed", "failed"):
            connection.execute(
                """INSERT INTO ledger_llm_previews(
                    id, profile_code, mode, provider, model, payload_json,
                    transaction_versions_json, fingerprint, status, expires_at, result_json
                ) VALUES (?, 'test', 'local', 'openai-compatible', 'historic', '[]',
                          '{}', 'historic-fingerprint', ?, '2026-01-01', '[]')""",
                (status, status),
            )
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM ledger_classifications WHERE id=?",
                    (identifier,),
                ).fetchone()
            )
            == classification_before
        )
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM ledger_audit_events")
        ] == audit_before
        previews = {
            row["id"]: dict(row) for row in connection.execute("SELECT * FROM ledger_llm_previews")
        }
    assert {key: row["status"] for key, row in previews.items()} == {
        "ready": "invalidated",
        "running": "invalidated",
        "executed": "executed",
        "failed": "failed",
    }
    assert all(row["request_context_json"] is None for row in previews.values())
    assert previews["executed"]["fingerprint"] == "historic-fingerprint"
    assert previews["executed"]["result_json"] == "[]"
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_schema_migration_checksum_changes_are_rejected(tmp_path):
    database = LedgerDatabase(tmp_path / "ledger.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=?",
            (MIGRATIONS[0].version,),
        )

    with pytest.raises(MigrationChecksumError, match="checksum mismatch"):
        database.initialize()


def test_existing_database_is_backed_up_before_schema_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('before')")

    database = LedgerDatabase(database_path)
    database.initialize()

    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "before"


def test_verified_backup_can_restore_the_database(tmp_path):
    database = LedgerDatabase(tmp_path / "ledger.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_providers(id, kind, display_name)
               VALUES ('before', 'test', 'Before')"""
        )
    backup_path = database.backup(tmp_path / "snapshot.sqlite3")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_providers(id, kind, display_name)
               VALUES ('after', 'test', 'After')"""
        )

    database.restore(backup_path)

    with database.connect() as connection:
        provider_ids = {row["id"] for row in connection.execute("SELECT id FROM ledger_providers")}
    assert provider_ids == {"before"}


def test_cli_backup_and_confirmed_restore(tmp_path):
    database_path = tmp_path / "ledger.db"
    backup_path = tmp_path / "backup.sqlite3"
    database = LedgerDatabase(database_path)
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_providers(id, kind, display_name)
               VALUES ('kept', 'test', 'Kept')"""
        )

    assert (
        main(
            [
                "--db",
                str(database_path),
                "database",
                "backup",
                "--output",
                str(backup_path),
            ]
        )
        == 0
    )
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO ledger_providers(id, kind, display_name)
               VALUES ('removed', 'test', 'Removed')"""
        )
    assert (
        main(
            [
                "--db",
                str(database_path),
                "database",
                "restore",
                str(backup_path),
                "--yes",
            ]
        )
        == 0
    )

    with database.connect() as connection:
        provider_ids = {row["id"] for row in connection.execute("SELECT id FROM ledger_providers")}
    assert provider_ids == {"kept"}


def test_json_export_contains_versioned_ledger_tables(tmp_path):
    database = LedgerDatabase(tmp_path / "ledger.db")
    database.initialize()
    destination = database.export_json(tmp_path / "export" / "ledger.json")

    import json

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["format"] == "account-eval-ledger-export-v1"
    assert "ledger_transactions" in payload["tables"]
    assert "schema_migrations" in payload["tables"]


def test_v7_database_upgrades_transaction_semantics_and_backfills_history(
    tmp_path,
):
    database = LedgerDatabase(tmp_path / "v7.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """
            INSERT INTO ledger_providers(id, kind, display_name)
            VALUES ('provider', 'file', 'Test');
            INSERT INTO ledger_accounts(
                id, provider_id, external_id, display_name, currency
            ) VALUES ('account', 'provider', 'remote', 'Testkonto', 'EUR');
            INSERT INTO ledger_source_transactions(
                id, provider_id, account_id, external_id,
                idempotency_key, raw_payload_json
            ) VALUES ('source', 'provider', 'account', 'booking',
                      'source-key', '{"legacy": true}');
            INSERT INTO ledger_transactions(
                id, source_transaction_id, account_id, booking_date,
                amount_minor, currency, purpose
            ) VALUES ('transaction', 'source', 'account', '2026-07-01',
                      1000, 'EUR', 'Altbestand');

            DROP VIEW v_ledger_active_transactions;
            DROP TABLE ledger_reconciliations;
            DROP TABLE ledger_balance_snapshots;
            DROP TABLE ledger_transaction_relations;
            DROP TABLE ledger_transaction_status_events;
            DROP TABLE ledger_source_transaction_revisions;
            DROP INDEX ix_ledger_transactions_booking_status;
            ALTER TABLE ledger_transactions DROP COLUMN booking_status;
            ALTER TABLE ledger_accounts DROP COLUMN last_balance_at;
            ALTER TABLE ledger_sync_runs DROP COLUMN updated_count;
            ALTER TABLE ledger_sync_account_runs DROP COLUMN updated_count;
            ALTER TABLE ledger_import_runs DROP COLUMN updated_count;
            DELETE FROM schema_migrations WHERE version=8;
            """
        )

    database.initialize()

    with database.connect() as connection:
        transaction = connection.execute(
            """SELECT booking_status FROM ledger_transactions
               WHERE id='transaction'"""
        ).fetchone()
        revisions = connection.execute(
            """SELECT count(*) FROM ledger_source_transaction_revisions
               WHERE source_transaction_id='source'"""
        ).fetchone()[0]
        events = connection.execute(
            """SELECT to_status, transaction_version
               FROM ledger_transaction_status_events
               WHERE transaction_id='transaction'"""
        ).fetchall()
    assert transaction["booking_status"] == "booked"
    assert revisions == 1
    assert [tuple(item) for item in events] == [("booked", 1)]
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_v8_database_upgrades_personal_organization_and_backfills_review(
    tmp_path,
):
    database = LedgerDatabase(tmp_path / "v8.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """
            INSERT INTO ledger_providers(id, kind, display_name)
            VALUES ('provider-v8', 'file', 'Test V8');
            INSERT INTO ledger_accounts(
                id, provider_id, external_id, display_name, currency
            ) VALUES ('account-v8', 'provider-v8', 'remote', 'Testkonto', 'EUR');
            INSERT INTO ledger_source_transactions(
                id, provider_id, account_id, external_id,
                idempotency_key, raw_payload_json
            ) VALUES ('source-v8', 'provider-v8', 'account-v8', 'booking',
                      'source-key-v8', '{}');
            INSERT INTO ledger_transactions(
                id, source_transaction_id, account_id, booking_date,
                amount_minor, currency, purpose, booking_status
            ) VALUES ('transaction-v8', 'source-v8', 'account-v8',
                      '2026-07-01', 1000, 'EUR', 'Altbestand', 'pending');

            DROP VIEW v_ledger_active_transactions;
            DROP TABLE ledger_bulk_preview_items;
            DROP TABLE ledger_bulk_previews;
            DROP TABLE ledger_review_items;
            DROP TABLE ledger_transaction_dimensions;
            DROP TABLE ledger_dimension_values;
            DROP TABLE ledger_dimensions;
            DROP TABLE ledger_transaction_tags;
            DROP TABLE ledger_tags;
            DROP INDEX ix_ledger_transactions_merchant;
            ALTER TABLE ledger_transactions DROP COLUMN merchant_id;
            DROP TABLE ledger_merchant_aliases;
            DROP TABLE ledger_merchants;
            ALTER TABLE ledger_accounts DROP COLUMN account_type;
            DELETE FROM schema_migrations WHERE version=9;
            """
        )

    database.initialize()

    with database.connect() as connection:
        account = connection.execute(
            "SELECT account_type FROM ledger_accounts WHERE id='account-v8'"
        ).fetchone()
        dimensions = {
            row["code"] for row in connection.execute("SELECT code FROM ledger_dimensions")
        }
        reviews = {
            row["item_type"]
            for row in connection.execute(
                """SELECT item_type FROM ledger_review_items
                   WHERE entity_id='transaction-v8'"""
            )
        }
    assert account["account_type"] == "checking"
    assert dimensions == {"person", "household", "property", "project"}
    assert reviews == {"new_transaction", "pending_transaction"}
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_v9_database_upgrades_versioned_analysis_contract(tmp_path):
    database = LedgerDatabase(tmp_path / "v9.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """
            DROP TABLE ledger_analysis_runs;
            DROP TABLE ledger_exchange_rates;
            DROP TABLE ledger_saved_analyses;
            DELETE FROM schema_migrations WHERE version=10;
            """
        )

    database.initialize()

    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name LIKE 'ledger_%'"""
            )
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version=10"
        ).fetchone()
    assert {
        "ledger_saved_analyses",
        "ledger_exchange_rates",
        "ledger_analysis_runs",
    }.issubset(tables)
    assert migration["name"] == "personalized_analysis"
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_v13_database_upgrades_notes_and_account_field_options(tmp_path):
    database = LedgerDatabase(tmp_path / "v13.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """
            DROP VIEW v_ledger_active_transactions;
            DROP TABLE ledger_account_field_options;
            ALTER TABLE ledger_transactions DROP COLUMN note;
            DELETE FROM schema_migrations WHERE version=14;
            """
        )

    database.initialize()

    with database.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ledger_transactions)")}
        options = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("ledger_account_field_options",),
        ).fetchone()
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version=14"
        ).fetchone()
        view = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name=?",
            ("v_ledger_active_transactions",),
        ).fetchone()
    assert "note" in columns
    assert options is not None
    assert migration["name"] == "gui_editing"
    assert "t.note" in view["sql"]


def test_v14_database_upgrades_classification_configuration(tmp_path):
    database = LedgerDatabase(tmp_path / "v14.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.executescript(
            """
            DROP TABLE ledger_account_stages;
            DROP TABLE ledger_account_profiles;
            DROP TABLE ledger_account_columns;
            ALTER TABLE ledger_hints DROP COLUMN account_id;
            ALTER TABLE ledger_rules DROP COLUMN account_id;
            ALTER TABLE ledger_rules DROP COLUMN version;
            ALTER TABLE ledger_dimension_values DROP COLUMN active;
            ALTER TABLE ledger_dimensions DROP COLUMN active;
            DELETE FROM schema_migrations WHERE version=15;
            """
        )

    database.initialize()

    with database.connect() as connection:
        dimensions = {row[1] for row in connection.execute("PRAGMA table_info(ledger_dimensions)")}
        values = {
            row[1] for row in connection.execute("PRAGMA table_info(ledger_dimension_values)")
        }
        rules = {row[1] for row in connection.execute("PRAGMA table_info(ledger_rules)")}
        hints = {row[1] for row in connection.execute("PRAGMA table_info(ledger_hints)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "active" in dimensions
    assert "active" in values
    assert {"version", "account_id"}.issubset(rules)
    assert "account_id" in hints
    assert {"ledger_account_columns", "ledger_account_profiles", "ledger_account_stages"}.issubset(
        tables
    )


def test_v16_database_preserves_legacy_bulk_previews_when_adding_composite_edits(tmp_path):
    database = LedgerDatabase(tmp_path / "v16.db")
    database.initialize()
    with database.connect() as connection:
        connection.executescript(
            """
            INSERT INTO ledger_providers(id, kind, display_name)
            VALUES ('provider-v16', 'manual', 'Manual');
            INSERT INTO ledger_accounts(id, provider_id, external_id, display_name, currency)
            VALUES ('account-v16', 'provider-v16', 'account', 'Cash', 'EUR');
            INSERT INTO ledger_source_transactions(
                id, provider_id, account_id, external_id, idempotency_key, raw_payload_json
            ) VALUES ('source-v16', 'provider-v16', 'account-v16', 'booking', 'source-v16', '{}');
            INSERT INTO ledger_transactions(
                id, source_transaction_id, account_id, booking_date,
                amount_minor, currency, purpose
            ) VALUES ('transaction-v16', 'source-v16', 'account-v16',
                      '2026-08-01', -100, 'EUR', 'Legacy');
            INSERT INTO ledger_bulk_previews(
                id, action, payload_json, transaction_count, version_fingerprint
            ) VALUES ('preview-v16', 'tag_add', '{"tag":"Review"}', 1, 'fingerprint');
            INSERT INTO ledger_bulk_preview_items(
                preview_id, transaction_id, expected_version
            ) VALUES ('preview-v16', 'transaction-v16', 1);
            DELETE FROM schema_migrations WHERE version=17;
            """
        )

    database.initialize()

    with database.connect() as connection:
        preview = connection.execute(
            "SELECT action, status FROM ledger_bulk_previews WHERE id='preview-v16'"
        ).fetchone()
        item = connection.execute(
            """SELECT expected_version FROM ledger_bulk_preview_items
               WHERE preview_id='preview-v16' AND transaction_id='transaction-v16'"""
        ).fetchone()
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ledger_bulk_previews'"
        ).fetchone()[0]
    assert tuple(preview) == ("tag_add", "prepared")
    assert item["expected_version"] == 1
    assert "edit_fields" in sql

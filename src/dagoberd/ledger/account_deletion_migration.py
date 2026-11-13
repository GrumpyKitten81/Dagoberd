"""Durable account-deletion previews and receipts, independent of deleted accounts."""

import sqlite3

ACCOUNT_DELETION_SCHEMA = """CREATE TABLE IF NOT EXISTS ledger_account_deletions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared', 'executed')),
    receipt_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT
)"""


def migrate_account_deletions(connection: sqlite3.Connection) -> None:
    connection.execute(ACCOUNT_DELETION_SCHEMA)

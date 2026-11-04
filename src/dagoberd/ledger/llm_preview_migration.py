from __future__ import annotations

import sqlite3


def migrate_llm_preview_context(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ledger_llm_previews)")}
    if "request_context_json" not in columns:
        connection.execute("ALTER TABLE ledger_llm_previews ADD COLUMN request_context_json TEXT")
    connection.execute(
        """UPDATE ledger_llm_previews SET status='invalidated'
           WHERE request_context_json IS NULL AND status IN ('ready', 'running')"""
    )

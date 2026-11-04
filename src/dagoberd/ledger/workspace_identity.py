from __future__ import annotations

import sqlite3


def migrate_workspace_identity(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS ledger_workspace_identity (
               singleton INTEGER PRIMARY KEY CHECK(singleton=1),
               workspace_id TEXT NOT NULL
           )"""
    )

from __future__ import annotations

import sqlite3

import pytest

from dagoberd.ledger.database import LedgerDatabase


@pytest.mark.parametrize("operation", ["initialize", "integrity_check"])
def test_database_operations_close_connections_before_return(tmp_path, monkeypatch, operation):
    database = LedgerDatabase(tmp_path / "ledger.db")
    if operation == "integrity_check":
        database.initialize()
    original_connect = sqlite3.connect
    connections = []

    def record_connection(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", record_connection)

    getattr(database, operation)()

    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")

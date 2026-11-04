"""Account-specific classification pipeline configuration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .repository import LedgerRepository

STAGES = ("rule", "history", "hint", "llm")


@dataclass(frozen=True, slots=True)
class StageSetting:
    stage: str
    enabled: bool


class PipelineConfig:
    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository

    def account_profiles(self, account_id: str) -> list[dict]:
        with self.repository.database.connect() as connection:
            self._account(connection, account_id)
            return self._account_profiles(connection, account_id)

    def set_account_profiles(self, account_id: str, profile_codes: list[str]) -> dict:
        if len(set(profile_codes)) != len(profile_codes):
            raise ValueError("Profile codes must be unique")
        with self.repository.database.connect() as connection:
            self._account(connection, account_id)
            profiles = []
            for code in profile_codes:
                profile = connection.execute(
                    "SELECT * FROM ledger_profiles WHERE code=?", (code,)
                ).fetchone()
                if profile is None:
                    raise KeyError(f"Unknown profile {code}")
                profiles.append(profile)
            before = self._account_profiles(connection, account_id)
            connection.execute(
                "DELETE FROM ledger_account_profiles WHERE account_id=?", (account_id,)
            )
            connection.executemany(
                "INSERT INTO ledger_account_profiles(account_id, profile_id, position) VALUES (?, ?, ?)",
                [
                    (account_id, profile["id"], position)
                    for position, profile in enumerate(profiles)
                ],
            )
            result = {
                "account_id": account_id,
                "profiles": [profile["code"] for profile in profiles],
            }
            self.repository._audit(
                connection, "account", account_id, "profiles_set", before, result
            )
            return result

    def stages(self, account_id: str) -> dict[str, bool]:
        with self.repository.database.connect() as connection:
            self._account(connection, account_id)
            return self._stages(connection, account_id)

    def set_stage(self, account_id: str, stage: str, enabled: bool) -> dict:
        if stage not in STAGES:
            raise ValueError(f"Unknown classification stage {stage}")
        with self.repository.database.connect() as connection:
            self._account(connection, account_id)
            before = self._stages(connection, account_id)[stage]
            connection.execute(
                """INSERT INTO ledger_account_stages(account_id, stage, enabled) VALUES (?, ?, ?)
                   ON CONFLICT(account_id, stage) DO UPDATE SET enabled=excluded.enabled""",
                (account_id, stage, int(enabled)),
            )
            result = {"account_id": account_id, "stage": stage, "enabled": enabled}
            self.repository._audit(
                connection, "account", account_id, "stage_set", {"enabled": before}, result
            )
            return result

    @staticmethod
    def _account(connection: sqlite3.Connection, account_id: str) -> None:
        if not connection.execute(
            "SELECT 1 FROM ledger_accounts WHERE id=?", (account_id,)
        ).fetchone():
            raise KeyError(f"Unknown account {account_id}")

    @staticmethod
    def _account_profiles(connection: sqlite3.Connection, account_id: str) -> list[dict]:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT p.code, p.name, p.description, ap.position
                   FROM ledger_account_profiles ap JOIN ledger_profiles p ON p.id=ap.profile_id
                   WHERE ap.account_id=? ORDER BY ap.position""",
                (account_id,),
            )
        ]

    @staticmethod
    def _stages(connection: sqlite3.Connection, account_id: str) -> dict[str, bool]:
        configured = {
            row["stage"]: bool(row["enabled"])
            for row in connection.execute(
                "SELECT stage, enabled FROM ledger_account_stages WHERE account_id=?",
                (account_id,),
            )
        }
        return {stage: configured.get(stage, True) for stage in STAGES}

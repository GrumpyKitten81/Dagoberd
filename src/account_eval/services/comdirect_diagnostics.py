"""Sanitized Comdirect diagnostics and explicit recovery guidance."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from account_eval.config import ComdirectConnectionConfig, comdirect_timed_session_secret
from account_eval.ledger.repository import LedgerRepository
from account_eval.providers.comdirect import ComdirectSessionState
from account_eval.secrets import SecretStore


class ComdirectDiagnosticsService:
    def __init__(
        self,
        repository: LedgerRepository,
        connection_name: str,
        config: ComdirectConnectionConfig,
        secrets: SecretStore,
    ) -> None:
        self.repository = repository
        self.connection_name = connection_name
        self.config = config
        self.secrets = secrets

    def diagnose(self) -> dict:
        credential_names = (
            self.config.username_secret,
            self.config.client_id_secret,
            self.config.client_secret_secret,
        )
        missing = [name for name in credential_names if not self.secrets.get_keyring(name)]
        session_status = "not_used"
        valid_until = None
        serialized = self.secrets.get_keyring(comdirect_timed_session_secret(self.connection_name))
        if self.config.session_mode == "keychain_timed" and serialized:
            try:
                state = ComdirectSessionState.from_json(serialized)
                authenticated_at = datetime.fromisoformat(state.authenticated_at)
                if authenticated_at.tzinfo is None:
                    authenticated_at = authenticated_at.replace(tzinfo=UTC)
                valid_until = (
                    authenticated_at + timedelta(hours=self.config.keychain_session_max_age_hours)
                ).isoformat()
                session_status = (
                    "valid"
                    if state.is_within(self.config.keychain_session_max_age_hours)
                    else "expired"
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                session_status = "invalid"
        elif self.config.session_mode == "keychain_timed":
            session_status = "missing"
        provider_name = f"Comdirect ({self.connection_name})"
        recent_runs = self.repository.list_sync_runs(
            "comdirect",
            provider_name=provider_name,
            limit=5,
        )
        local_accounts = [
            account
            for account in self.repository.list_accounts(enabled_only=False)
            if account["provider_name"] == provider_name
        ]
        account_counts: dict[str, int] = {}
        for account in local_accounts:
            kind = str(account["account_type"])
            account_counts[kind] = account_counts.get(kind, 0) + 1
        failed = next((run for run in recent_runs if run["status"] == "failed"), None)
        if not self.config.enabled:
            next_action = "enable_comdirect"
        elif missing:
            next_action = "configure_secrets"
        elif self.config.session_mode == "keychain_timed" and session_status in {
            "missing",
            "invalid",
        }:
            next_action = "authenticate"
        elif session_status == "expired":
            next_action = "reauthenticate"
        elif failed:
            next_action = "recover_failed_sync"
        else:
            next_action = "sync"
        return {
            "provider": "comdirect",
            "connection": self.connection_name,
            "read_only": True,
            "enabled": self.config.enabled,
            "credentials_ready": not missing,
            "missing_secret_references": missing,
            "session": {
                "status": session_status,
                "mode": self.config.session_mode,
                "valid_until": valid_until,
                "mcp_sync_allowed": self.config.allow_mcp_sync,
            },
            "recent_sync_runs": recent_runs,
            "recoverable_sync_run_id": failed["id"] if failed else None,
            "accounts": {
                "local": len(local_accounts),
                "by_type": account_counts,
                "supported_types": ["checking", "savings"],
                "skipped_product_types": ["credit_card", "depot", "unknown"],
            },
            "next_action": next_action,
        }

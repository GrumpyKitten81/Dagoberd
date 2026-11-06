"""Keyring-first local credential storage with explicit fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

SECRET_ALIASES = {
    "comdirect.username": "COMDIRECT_USERNAME",
    "comdirect.client-id": "COMDIRECT_CLIENT_ID",
    "comdirect.client-secret": "COMDIRECT_CLIENT_SECRET",
    "comdirect.timed-session": "COMDIRECT_TIMED_SESSION",
    "llm.api-key": "DAGOBERD_LLM_API_KEY",
    "backup.password": "DAGOBERD_BACKUP_PASSWORD",
    "smtp.username": "DAGOBERD_SMTP_USERNAME",
    "smtp.password": "DAGOBERD_SMTP_PASSWORD",
}


class SecretStoreError(RuntimeError):
    pass


def canonical_secret_name(name: str) -> str:
    return SECRET_ALIASES.get(name, name)


class SecretStore:
    def __init__(
        self,
        service: str = "dagoberd",
        dotenv_path: str | Path | None = None,
    ) -> None:
        from dagoberd.workspaces import CURRENT_WORKSPACE

        loaded = CURRENT_WORKSPACE.get()
        self.scoped = bool(loaded and loaded.config.workspace.id)
        if self.scoped and loaded is not None:
            service = f"{service}.workspace.{loaded.config.workspace.id}"
            dotenv_path = loaded.project_path.with_name(".env")
        self.service = service
        self.dotenv_path = Path(dotenv_path or ".env")

    def get(self, name: str) -> str | None:
        key = canonical_secret_name(name)
        try:
            import keyring

            value = keyring.get_password(self.service, key)
            if value:
                return value
        except Exception:
            pass
        value = None if self.scoped else os.environ.get(key)
        if value:
            return value
        try:
            from dotenv import dotenv_values

            return dotenv_values(self.dotenv_path, interpolate=not self.scoped).get(key)
        except ImportError:
            pass
        return None

    def get_keyring(self, name: str) -> str | None:
        """Read a value exclusively from the operating-system keyring."""
        key = canonical_secret_name(name)
        try:
            import keyring

            return keyring.get_password(self.service, key) or None
        except Exception:
            return None

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("Secret value must not be empty")
        key = canonical_secret_name(name)
        try:
            import keyring

            keyring.set_password(self.service, key, value)
        except Exception as error:
            raise SecretStoreError(
                "Secret could not be stored in the operating-system keyring"
            ) from error

    def delete(self, name: str) -> bool:
        """Delete a keyring value; environment/.env fallbacks remain untouched."""
        key = canonical_secret_name(name)
        try:
            import keyring

            keyring.delete_password(self.service, key)
            return True
        except Exception:
            return False

    def keyring_status(self) -> tuple[bool, str]:
        try:
            import keyring

            backend = keyring.get_keyring()
            return True, backend.__class__.__name__
        except Exception as error:
            return False, str(error)


def get_secret(name: str, service: str = "dagoberd") -> str | None:
    return SecretStore(service=service).get(name)

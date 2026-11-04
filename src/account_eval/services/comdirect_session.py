"""Non-interactive Comdirect session and synchronization operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from account_eval.config import ComdirectConnectionConfig, comdirect_timed_session_secret
from account_eval.ledger.repository import LedgerRepository
from account_eval.providers.comdirect import ComdirectProvider, ComdirectSessionState
from account_eval.secrets import SecretStore
from account_eval.services.comdirect_sync import ComdirectSyncService, SyncResult

TanCallback = Callable[[], str]
PhotoTanCallback = Callable[[bytes], str]


def new_comdirect_provider(
    connection: ComdirectConnectionConfig,
    pin: str,
    *,
    push_tan: TanCallback,
    photo_tan: PhotoTanCallback,
    sms_tan: TanCallback,
) -> ComdirectProvider:
    return ComdirectProvider.from_config(
        connection,
        SecretStore(),
        pin,
        push_tan_callback=push_tan,
        photo_tan_callback=photo_tan,
        sms_tan_callback=sms_tan,
    )


def synchronize(
    repository: LedgerRepository,
    connection_name: str,
    connection: ComdirectConnectionConfig,
    pin: str,
    *,
    push_tan: TanCallback,
    photo_tan: PhotoTanCallback,
    sms_tan: TanCallback,
    account_ids: list[str] | None = None,
    booking_from: str | None = None,
    booking_to: str | None = None,
    resume: str | None = None,
) -> SyncResult:
    provider = new_comdirect_provider(
        connection, pin, push_tan=push_tan, photo_tan=photo_tan, sms_tan=sms_tan
    )
    if connection.session_mode == "ephemeral":
        provider.login()
        try:
            return _run(
                repository, provider, connection_name, account_ids, booking_from, booking_to, resume
            )
        finally:
            provider.revoke()
    state = _load_timed(connection_name, connection)
    provider.restore_session(state)
    result = _run(
        repository, provider, connection_name, account_ids, booking_from, booking_to, resume
    )
    _store_timed(connection_name, connection, provider.export_session())
    return result


def _run(
    repository: LedgerRepository,
    provider: ComdirectProvider,
    connection_name: str,
    account_ids: list[str] | None,
    booking_from: str | None,
    booking_to: str | None,
    resume: str | None,
) -> SyncResult:
    return ComdirectSyncService(repository, provider, connection_name).synchronize(
        account_ids=account_ids,
        booking_from=date.fromisoformat(booking_from) if booking_from else None,
        booking_to=date.fromisoformat(booking_to) if booking_to else None,
        resume_sync_run_id=resume,
        interactive=False,
    )


def _load_timed(name: str, connection: ComdirectConnectionConfig) -> ComdirectSessionState:
    serialized = SecretStore().get_keyring(comdirect_timed_session_secret(name))
    if not serialized:
        raise ValueError(f"Comdirect authentication required; run local auth comdirect {name}")
    try:
        state = ComdirectSessionState.from_json(serialized)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "Invalid or expired Comdirect session; authenticate locally again"
        ) from error
    if state.is_within(connection.keychain_session_max_age_hours):
        return state
    raise ValueError(f"Comdirect session expired; run local auth comdirect {name}")


def _store_timed(
    name: str, connection: ComdirectConnectionConfig, state: ComdirectSessionState
) -> None:
    if not state.is_within(connection.keychain_session_max_age_hours):
        raise ValueError("The Comdirect session has expired and will not be stored")
    SecretStore().set(comdirect_timed_session_secret(name), state.to_json())


def timed_session_expiry(state: ComdirectSessionState, maximum_age_hours: int) -> datetime:
    authenticated_at = datetime.fromisoformat(state.authenticated_at)
    if authenticated_at.tzinfo is None:
        authenticated_at = authenticated_at.replace(tzinfo=UTC)
    return authenticated_at + timedelta(hours=maximum_age_hours)
